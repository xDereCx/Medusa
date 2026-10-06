# -*- coding: utf-8 -*-
"""Titulky.com subtitle provider (Czech/Slovak)."""
from __future__ import unicode_literals

import io
import json
import logging
import os
import re
import time
import zipfile
from datetime import date

from babelfish import Language

from guessit import guessit

from medusa import app

from requests import Session

from subliminal.exceptions import AuthenticationError, ProviderError
from subliminal.matches import guess_matches
from subliminal.providers import Provider
from subliminal.subtitle import Subtitle, fix_line_ending
from subliminal.utils import sanitize
from subliminal.video import Episode, Movie

logger = logging.getLogger(__name__)

#: Mapping of titulky.com language flags to babelfish languages
LANGUAGE_MAP = {
    'CZ': Language.fromalpha2('cs'),
    'SK': Language.fromalpha2('sk'),
}

#: Maximum time (seconds) we're willing to wait out titulky.com's download countdown
MAX_WAIT_TIME = 30

#: row in the fulltext search result table
ROW_RE = re.compile(r'<tr class="r(.+?)</tr>', re.IGNORECASE | re.DOTALL)
LINK_FILE_RE = re.compile(r'[^<]+<td[^<]+<a href="(?P<data>\S+)\.htm"', re.IGNORECASE | re.DOTALL)
SUB_ID_RE = re.compile(r'[^<]+<td[^<]+<a href="[\w-]+-(?P<data>\d+)\.htm"', re.IGNORECASE | re.DOTALL)
TITLE_RE = re.compile(r'[^<]+<td[^<]+<a[^>]+>(<div[^>]+>)?(?P<data>[^<]+)', re.IGNORECASE | re.DOTALL)
VERSION_RE = re.compile(r'((.+?)</td>)[^>]+>[^<]*<a(.+?)title="(?P<data>[^"]+)"', re.IGNORECASE | re.DOTALL)
SEASON_EPISODE_RE = re.compile(r'((.+?)</td>){2}[^>]+>(?P<data>[^<]+)', re.IGNORECASE | re.DOTALL)
YEAR_RE = re.compile(r'((.+?)</td>){3}[^>]+>(?P<data>[^<]+)', re.IGNORECASE | re.DOTALL)
DOWN_COUNT_RE = re.compile(r'((.+?)</td>){4}[^>]+>(?P<data>[^<]+)', re.IGNORECASE | re.DOTALL)
LANG_RE = re.compile(r'((.+?)</td>){5}[^>]+><img alt="(?P<data>\w{2})"', re.IGNORECASE | re.DOTALL)

SE_LABEL_RE = re.compile(r'S(?P<season>\d{1,2})E(?P<episode>\d{1,3})', re.IGNORECASE)
S_ONLY_LABEL_RE = re.compile(r'^S(?P<season>\d{1,2})$', re.IGNORECASE)

CAPTCHA_MARKER_RE = re.compile(r'\./(captcha/captcha\.php)', re.IGNORECASE | re.DOTALL)
WAIT_TIME_RE = re.compile(r'CountDown\((\d+)\)', re.IGNORECASE | re.DOTALL)
DOWNLINK_RE = re.compile(r'<a.+id="downlink" href="([^"]+)"', re.IGNORECASE | re.DOTALL)

#: used to pick the right file out of a season-pack archive
EPISODE_IN_FILENAME_RE = re.compile(r'(?:^|[^a-z0-9])e0*(?P<episode>\d{1,3})(?:[^a-z0-9]|$)', re.IGNORECASE)
SUBTITLE_EXTENSIONS = ('.srt', '.sub', '.ass')


class TitulkySubtitle(Subtitle):
    """Titulky.com Subtitle."""

    provider_name = 'titulky'

    def __init__(self, language, hearing_impaired, page_link, series, season, episode, title,
                 year, sub_id, link_file, release, is_pack=False):
        super(TitulkySubtitle, self).__init__(language, str(sub_id), hearing_impaired=hearing_impaired,
                                              page_link=page_link)
        self.series = series
        self.season = season
        self.episode = episode
        self.title = title
        self.year = year
        self.sub_id = sub_id
        self.link_file = link_file
        self.release = release
        self.is_pack = is_pack

    def get_matches(self, video):
        matches = set()

        if isinstance(video, Episode):
            if video.series and self.series and sanitize(self.series) == sanitize(video.series):
                matches.add('series')
            if video.season and self.season and self.season == video.season:
                matches.add('season')
            # a season-pack entry isn't tied to one specific episode until it's downloaded
            if video.episode and self.episode and self.episode == video.episode:
                matches.add('episode')
            matches |= guess_matches(video, guessit(self.release or self.title, {'type': 'episode'}))
        elif isinstance(video, Movie):
            if video.title and self.title and sanitize(self.title) == sanitize(video.title):
                matches.add('title')
            if video.year and self.year and video.year == self.year:
                matches.add('year')
            matches |= guess_matches(video, guessit(self.release or self.title, {'type': 'movie'}))

        return matches


class TitulkyProvider(Provider):
    """Titulky.com Provider."""

    languages = {LANGUAGE_MAP['CZ'], LANGUAGE_MAP['SK']}
    video_types = (Episode, Movie)
    server_url = 'https://www.titulky.com'

    #: titulky.com's VIP account gets 25 fast downloads + 25 from the premium server per day
    #: before falling back to a captcha; this keeps Medusa's own usage well under that so there's
    #: still room left for downloads made directly through the Kodi addon on the same account.
    DAILY_DOWNLOAD_LIMIT = 20

    def __init__(self, username=None, password=None):
        if any((username, password)) and not all((username, password)):
            raise ValueError('Username and password must be specified')

        self.username = username
        self.password = password
        self.logged_in = False
        self.session = None

    def initialize(self):
        self.session = Session()
        self.session.headers['User-Agent'] = self.user_agent

        if self.username and self.password:
            self.login()

    def terminate(self):
        self.session.close()

    def login(self):
        logger.info('Logging in to titulky.com')
        data = {
            'Login': self.username,
            'Password': self.password,
            'foreverlog': '0',
            'Detail2': '',
        }
        r = self.session.post(self.server_url + '/index.php', data=data,
                              headers={'Referer': self.server_url}, timeout=10)
        r.raise_for_status()

        if 'BadLogin' in r.text:
            raise AuthenticationError('Login failed, check your username/password')

        self.logged_in = True
        logger.info('Logged in to titulky.com')

    def _normalize_title(self, title):
        # drop anything in brackets/parentheses, titulky.com indexes clean titles
        return re.sub(r'(\[|\().+?(\]|\))', '', title).strip()

    def _parse_search_results(self, content):
        results = []
        for match in ROW_RE.finditer(content):
            row = match.group(1)
            try:
                link_file = LINK_FILE_RE.search(row).group('data')
                sub_id = SUB_ID_RE.search(row).group('data')
                title = TITLE_RE.search(row).group('data')
            except AttributeError:
                continue

            version_match = VERSION_RE.search(row)
            release = version_match.group('data') if version_match else title

            se_match = SEASON_EPISODE_RE.search(row)
            season_and_episode = se_match.group('data').strip() if se_match else None
            if season_and_episode == '&nbsp;':
                season_and_episode = None

            year_match = YEAR_RE.search(row)
            year = year_match.group('data').strip() if year_match else None
            if year == '&nbsp;' or not year or not year.isdigit():
                year = None
            else:
                year = int(year)

            lang_match = LANG_RE.search(row)
            if not lang_match or lang_match.group('data').upper() not in LANGUAGE_MAP:
                continue
            language = LANGUAGE_MAP[lang_match.group('data').upper()]

            results.append({
                'link_file': link_file,
                'sub_id': sub_id,
                'title': title,
                'release': release,
                'season_and_episode': season_and_episode,
                'year': year,
                'language': language,
            })

        return results

    def _search(self, search_title):
        url = self.server_url + '/index.php'
        params = {'Fulltext': search_title, 'FindUser': ''}
        logger.debug('Searching titulky.com for %r', search_title)

        r = self.session.get(url, params=params, timeout=10)
        r.raise_for_status()

        return self._parse_search_results(r.text)

    def query(self, series, season, episode, year=None, title=None):
        is_episode = series is not None
        subtitles = []

        if is_episode:
            search_title = self._normalize_title('{} S{:02d}E{:02d}'.format(series, season, episode))
            for result in self._search(search_title):
                se_match = SE_LABEL_RE.match(result['season_and_episode'] or '')
                if not se_match or int(se_match.group('season')) != season or int(se_match.group('episode')) != episode:
                    continue
                subtitles.append(TitulkySubtitle(
                    result['language'], False, self.server_url + '/' + result['link_file'] + '.htm',
                    series, season, episode, result['title'], result['year'],
                    result['sub_id'], result['link_file'], result['release'], is_pack=False))

            # supplemental search for whole-season packs, which titulky.com uploaders
            # usually tag with just the season number instead of a specific episode
            pack_search_title = self._normalize_title('{} S{:02d}'.format(series, season))
            for result in self._search(pack_search_title):
                s_match = S_ONLY_LABEL_RE.match(result['season_and_episode'] or '')
                if not s_match or int(s_match.group('season')) != season:
                    continue
                subtitles.append(TitulkySubtitle(
                    result['language'], False, self.server_url + '/' + result['link_file'] + '.htm',
                    series, season, episode, result['title'], result['year'],
                    result['sub_id'], result['link_file'], result['release'], is_pack=True))
        else:
            search_title = self._normalize_title(title)
            for result in self._search(search_title):
                if result['season_and_episode']:
                    # has a season/episode label, so it's a series result, not a movie
                    continue
                subtitles.append(TitulkySubtitle(
                    result['language'], False, self.server_url + '/' + result['link_file'] + '.htm',
                    None, None, None, result['title'], result['year'],
                    result['sub_id'], result['link_file'], result['release'], is_pack=False))

        return subtitles

    def list_subtitles(self, video, languages):
        if isinstance(video, Episode):
            subtitles = self.query(video.series, video.season, video.episode, video.year)
        else:
            subtitles = self.query(None, None, None, video.year, title=video.title)

        return [s for s in subtitles if s.language in languages]

    def _get_download_page(self, sub_id, referer, code=None):
        if code is None:
            params = {'R': str(int(time.time())), 'titulky': sub_id, 'histstamp': '', 'zip': 'z'}
            r = self.session.get(self.server_url + '/idown.php', params=params,
                                 headers={'Referer': referer}, timeout=10)
        else:
            data = {'downkod': code, 'titulky': sub_id, 'zip': 'z', 'securedown': '2', 'histstamp': ''}
            r = self.session.post(self.server_url + '/idown.php', data=data,
                                  headers={'Referer': referer}, timeout=10)
        r.raise_for_status()
        return r.text

    def _pick_episode_file(self, namelist, episode):
        candidates = [n for n in namelist if n.lower().endswith(SUBTITLE_EXTENSIONS)]
        if not candidates:
            raise ProviderError('No subtitle file found in the downloaded archive')

        if len(candidates) == 1 or episode is None:
            return candidates[0]

        # season pack: try to find the file matching the requested episode number
        for name in candidates:
            match = EPISODE_IN_FILENAME_RE.search(name)
            if match and int(match.group('episode')) == episode:
                return name

        for name in candidates:
            guess = guessit(name, {'type': 'episode'})
            if guess.get('episode') == episode:
                return name

        logger.warning('Could not identify episode %s inside season pack, using first file %s', episode,
                       candidates[0])
        return candidates[0]

    def _quota_path(self):
        return os.path.join(app.CACHE_DIR, 'titulky_download_quota.json')

    def _read_quota_count(self):
        today = date.today().isoformat()
        try:
            with open(self._quota_path()) as f:
                state = json.load(f)
            if state.get('date') == today:
                return state.get('count', 0)
        except (OSError, ValueError):
            pass
        return 0

    def _increment_quota_count(self):
        today = date.today().isoformat()
        count = self._read_quota_count() + 1
        with open(self._quota_path(), 'w') as f:
            json.dump({'date': today, 'count': count}, f)
        return count

    def download_subtitle(self, subtitle):
        used = self._read_quota_count()
        if used >= self.DAILY_DOWNLOAD_LIMIT:
            raise ProviderError('Daily titulky.com download limit ({}) already reached ({} used), '
                                'skipping to leave quota for manual downloads'.format(
                                    self.DAILY_DOWNLOAD_LIMIT, used))

        content = self._get_download_page(subtitle.sub_id, subtitle.page_link)

        if CAPTCHA_MARKER_RE.search(content):
            raise ProviderError('titulky.com requires solving a CAPTCHA to download this subtitle')

        wait_match = WAIT_TIME_RE.search(content)
        if wait_match:
            wait_time = min(int(wait_match.group(1)), MAX_WAIT_TIME)
            logger.debug('Waiting %s seconds before downloading', wait_time)
            time.sleep(wait_time)

        link_match = DOWNLINK_RE.search(content)
        if not link_match:
            raise ProviderError('Could not find the final download link')

        download_url = self.server_url + link_match.group(1)
        r = self.session.get(download_url, headers={'Referer': self.server_url + '/idown.php'}, timeout=10)
        r.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            filename = self._pick_episode_file(zf.namelist(), subtitle.episode)
            subtitle.content = fix_line_ending(zf.read(filename))

        self._increment_quota_count()

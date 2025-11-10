import spotipy
from spotipy.oauth2 import SpotifyOAuth
import json
import requests
import re
import os
import time
import urllib3
from requests.adapters import HTTPAdapter
import random
from typing import Dict, Set, Tuple, Optional, List

# Configuration Constants
API_CALL_LIMIT_PER_30SEC = 60
BATCH_SIZE = 100
MAX_PLAYLISTS_PER_QUERY = 850
MAX_SPOTIFY_OFFSET = 1000
SEARCH_LIMIT = 50
MAX_RETRIES = 3
MAIN_MAX_RETRIES = 5

client_id = 'your_client_id'
client_secret = 'your_client_secret'


class RateLimiter:
    """Manages API rate limiting with a sliding window approach"""

    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.call_timestamps = []

    def wait_if_needed(self):
        """Enforce rate limiting using a sliding window"""
        current_time = time.time()

        # Remove timestamps outside the current window
        self.call_timestamps = [ts for ts in self.call_timestamps
                               if current_time - ts < self.window_seconds]

        # If at limit, wait until the oldest call expires
        if len(self.call_timestamps) >= self.max_calls:
            oldest_call = self.call_timestamps[0]
            wait_time = self.window_seconds - (current_time - oldest_call)
            if wait_time > 0:
                print(f"Rate limit reached. Waiting for {wait_time:.2f} seconds.")
                time.sleep(wait_time + 0.1)  # Add small buffer
                self.call_timestamps = []  # Reset after waiting

        # Record this call
        self.call_timestamps.append(time.time())


class SpotifyPlaylistAnalyzer:
    """Main class for analyzing Spotify playlists and creating compilations"""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.rate_limiter = RateLimiter(API_CALL_LIMIT_PER_30SEC, 30)
        self.sp = None
        self.user_id = None
        self.cache_file = '.cache'

    def initialize(self):
        """Initialize Spotify client and authenticate"""
        if not self.client_id or not self.client_secret:
            raise ValueError("Spotify API credentials are not configured properly")

        session = self._build_session()
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=self.client_id,
                client_secret=self.client_secret,
                redirect_uri="http://127.0.0.1:8000/callback/",
                scope="playlist-modify-private"
            ),
            requests_session=session
        )

        # Validate authentication
        try:
            user = self.sp.current_user()
            if not user or 'id' not in user:
                raise ValueError("Failed to authenticate with Spotify")
            self.user_id = user["id"]
            print(f"✅ Successfully authenticated as: {user.get('display_name', self.user_id)}")
        except requests.exceptions.HTTPError as e:
            raise ValueError(f"Authentication failed: {str(e)}")

    @staticmethod
    def _build_session():
        """Build requests session with retry logic"""
        session = requests.Session()
        retry = urllib3.Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            respect_retry_after_header=True
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    @staticmethod
    def normalize_track_name(track_name: Optional[str]) -> str:
        """Normalize track name by removing version suffixes"""
        if not track_name:
            return ''
        track_name = str(track_name)
        if '-' in track_name:
            return track_name.split('-')[0].strip()
        return track_name.strip()

    @staticmethod
    def create_dedup_key(artist_name: Optional[str], track_name: Optional[str]) -> str:
        """Create a unique key for deduplication"""
        artist_name = str(artist_name) if artist_name else 'Unknown Artist'
        track_name = str(track_name) if track_name else 'Unknown Track'
        normalized_track = SpotifyPlaylistAnalyzer.normalize_track_name(track_name)
        return f"{artist_name.lower()}|||{normalized_track.lower()}"

    def handle_http_error(self, e: requests.exceptions.HTTPError, context: str = "") -> str:
        """Centralized HTTP error handling"""
        if not hasattr(e, 'response') or e.response is None:
            print(f"HTTP Error{' in ' + context if context else ''}: {str(e)}")
            return 'error'

        status_code = e.response.status_code
        context_str = f' in {context}' if context else ''

        if status_code == 429:
            retry_after = int(e.response.headers.get('Retry-After', 30))
            print(f"Rate limit exceeded{context_str}. Waiting for {retry_after} seconds.")
            time.sleep(retry_after)
            return 'retry'
        elif status_code == 400:
            print(f"Bad request{context_str}: {str(e)}")
            return 'skip'
        elif status_code in [500, 502, 503, 504]:
            print(f"Server error{context_str}: {str(e)}")
            return 'retry'
        elif status_code == 401:
            print(f"Authentication error{context_str}: {str(e)}")
            return 'auth_error'
        elif status_code == 403:
            print(f"Forbidden{context_str}: {str(e)}")
            return 'skip'

        print(f"HTTP Error{context_str}: {str(e)}")
        return 'error'

    def safe_api_call(self, func, *args, **kwargs):
        """Wrapper for API calls with rate limiting"""
        self.rate_limiter.wait_if_needed()
        return func(*args, **kwargs)

    def load_global_processed_playlists(self) -> Tuple[Set[str], Dict[str, Set[str]]]:
        """Load globally processed playlists and their counted tracks"""
        filename = f'progress_{self.user_id}_GLOBAL_MASTER.json'
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
                playlist_tracks = {}
                for playlist_id, tracks in data.get('playlist_counted_tracks', {}).items():
                    playlist_tracks[playlist_id] = set(tracks) if isinstance(tracks, list) else set()
                return set(data.get('processed_playlists_global', [])), playlist_tracks
        except FileNotFoundError:
            return set(), {}
        except json.JSONDecodeError as e:
            print(f"Corrupted global playlist progress file: {filename}, resetting... Error: {str(e)}")
            return set(), {}

    def save_global_processed_playlists(self, processed_playlists: Set[str],
                                       playlist_tracks: Dict[str, Set[str]]) -> bool:
        """Atomically save global processed playlists"""
        filename = f'progress_{self.user_id}_GLOBAL_MASTER.json'
        temp_filename = f"{filename}.tmp"

        playlist_tracks_serializable = {
            playlist_id: list(tracks)
            for playlist_id, tracks in playlist_tracks.items()
        }

        data = {
            'processed_playlists_global': list(processed_playlists),
            'playlist_counted_tracks': playlist_tracks_serializable
        }

        try:
            with open(temp_filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(temp_filename, filename)
            return True
        except (IOError, OSError) as e:
            print(f"File error while saving global state: {str(e)}")
            if os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except OSError:
                    pass
            return False

    def save_progress(self, data: dict, filename: str) -> bool:
        """Atomically save progress data"""
        temp_filename = f"{filename}.tmp"

        # Convert sets to lists for JSON serialization
        data_copy = data.copy()
        if 'all_tracks' in data_copy:
            data_copy['all_tracks'] = list(data_copy['all_tracks'])

        try:
            with open(temp_filename, 'w', encoding='utf-8') as f:
                json.dump(data_copy, f, ensure_ascii=False, indent=4)
            os.replace(temp_filename, filename)
            return True
        except (IOError, OSError) as e:
            print(f"File error while saving progress: {str(e)}")
            if os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except OSError:
                    pass
            return False

    def save_all_state(self, query: str, query_state: dict,
                      master_processed: Set[str], master_counted: Dict[str, Set[str]]) -> bool:
        """Save both query and global state atomically"""
        queryfile = f'progress_{self.user_id}_{query}.json'
        query_ok = self.save_progress(query_state, queryfile)
        master_ok = self.save_global_processed_playlists(master_processed, master_counted)

        if not (query_ok and master_ok):
            print(f"Atomic save failed for {'query' if not query_ok else 'master'} progress.")
        return query_ok and master_ok

    def load_progress(self, filename: str) -> dict:
        """Load progress data from file"""
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)

                # Convert lists back to sets
                if 'all_tracks' in data and isinstance(data['all_tracks'], list):
                    data['all_tracks'] = set(data['all_tracks'])

                # Ensure all required fields exist
                data.setdefault('processed_playlists', [])
                data.setdefault('playlist_progress', {})
                data.setdefault('track_data', {})
                data.setdefault('search_offset', 0)

                # Migrate old data format
                self._migrate_track_data(data)

                return data
        except FileNotFoundError:
            return self._create_empty_progress()
        except json.JSONDecodeError as e:
            print(f"Corrupted file: {filename}, resetting save. Error: {str(e)}")
            return self._create_empty_progress()

    @staticmethod
    def _create_empty_progress() -> dict:
        """Create empty progress structure"""
        return {
            'processed_playlists': [],
            'all_tracks': set(),
            'playlist_progress': {},
            'track_data': {},
            'search_offset': 0
        }

    def _migrate_track_data(self, data: dict):
        """Migrate old track data format to new format"""
        for playlist_id, progress in data.get('playlist_progress', {}).items():
            progress.setdefault('completed', False)
            progress.setdefault('matched_query', True)

        for dedup_key, track_info in data.get('track_data', {}).items():
            # Migrate track_id to track_ids
            if 'track_id' in track_info and 'track_ids' not in track_info:
                old_track_id = track_info['track_id']
                track_info['track_ids'] = {old_track_id: track_info.get('count', 1)}
                del track_info['track_id']
            elif 'track_ids' not in track_info:
                track_info['track_ids'] = {}

            # Migrate release_year to release_years
            if 'release_year' in track_info and 'release_years' not in track_info:
                old_release_year = track_info['release_year']
                track_info['release_years'] = {old_release_year: track_info.get('count', 1)}
            elif 'release_years' not in track_info:
                track_info['release_years'] = {}

            # Migrate release_date to release_dates
            if 'release_date' in track_info and 'release_dates' not in track_info:
                old_release_date = track_info['release_date']
                track_info['release_dates'] = {old_release_date: track_info.get('count', 1)}
            elif 'release_dates' not in track_info:
                track_info['release_dates'] = {}

    def process_playlist(self, which: int, total: int, item: dict, all_tracks: Set[str],
                        playlist_progress: dict, track_data: dict, query: str,
                        start_year: Optional[int], end_year: Optional[int],
                        playlist_counted_tracks_global: Dict[str, Set[str]],
                        processed_playlists_global: Set[str]) -> str:
        """Process a single playlist and extract tracks"""

        # Validate playlist item
        if not item or 'id' not in item:
            return 'skip'

        playlist_id = item['id']
        playlist_name = item.get('name') or 'Unknown Playlist'

        # Check if playlist name matches query (FIXED: single backslash in raw f-string)
        if not re.search(rf'{re.escape(query.lower())}', playlist_name.lower(), re.IGNORECASE):
            if playlist_id not in playlist_progress:
                playlist_progress[playlist_id] = {
                    'offset': 0,
                    'track_info': [],
                    'completed': True,
                    'matched_query': False
                }
            return 'skipped'

        # Initialize playlist progress if needed
        if playlist_id not in playlist_progress:
            playlist_progress[playlist_id] = {
                'offset': 0,
                'track_info': [],
                'completed': False,
                'matched_query': True
            }

        # Skip if already completed
        if playlist_progress[playlist_id].get('completed', False):
            print(f"Playlist {playlist_name} already fully processed, skipping.")
            return 'completed'

        # Initialize global tracking
        if playlist_id not in playlist_counted_tracks_global:
            playlist_counted_tracks_global[playlist_id] = set()

        retry_count = 0
        fully_processed = False

        while retry_count < MAX_RETRIES:
            offset = playlist_progress[playlist_id]['offset']

            try:
                tracks = self.safe_api_call(self.sp.playlist_tracks, playlist_id, offset=offset)

                if not tracks or 'items' not in tracks or not tracks['items']:
                    print(f"Warning: No tracks returned for {playlist_name}")
                    return 'incomplete'

            except requests.exceptions.HTTPError as e:
                action = self.handle_http_error(e, f"playlist {playlist_name}")
                if action == 'retry':
                    retry_count += 1
                    continue
                elif action in ['skip', 'auth_error']:
                    playlist_progress[playlist_id]['completed'] = True
                    return 'completed'
                else:
                    retry_count += 1
                    continue
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                print(f"Network error for playlist {playlist_name}: {type(e).__name__}")
                retry_count += 1
                time.sleep(5)
                continue
            except requests.exceptions.RequestException as e:
                print(f"Network error for {playlist_name}: {str(e)}")
                retry_count += 1
                continue

            # Process tracks
            for item in tracks['items']:
                self._process_track_item(
                    item, playlist_name, playlist_id, playlist_counted_tracks_global,
                    track_data, all_tracks, playlist_progress, start_year, end_year, which, total
                )

            # Save progress after each batch
            if tracks.get('items'):
                self.save_all_state(
                    query,
                    {
                        'processed_playlists': list(playlist_progress.keys()),
                        'all_tracks': all_tracks,
                        'playlist_progress': playlist_progress,
                        'track_data': track_data,
                        'search_offset': 0
                    },
                    processed_playlists_global,
                    playlist_counted_tracks_global
                )

            # Check if more tracks exist
            if tracks and tracks.get('next'):
                playlist_progress[playlist_id]['offset'] += len(tracks['items'])
            else:
                fully_processed = True
                break

        if fully_processed:
            playlist_progress[playlist_id]['offset'] = 0
            playlist_progress[playlist_id]['completed'] = True
            return 'completed'
        else:
            print(f"⚠️  Warning: Playlist {playlist_name} incomplete after {MAX_RETRIES} retries.")
            return 'incomplete'

    def _process_track_item(self, item: dict, playlist_name: str, playlist_id: str,
                           playlist_counted_tracks_global: Dict[str, Set[str]],
                           track_data: dict, all_tracks: Set[str], playlist_progress: dict,
                           start_year: Optional[int], end_year: Optional[int],
                           which: int, total: int):
        """Process a single track item from playlist"""
        try:
            if not item:
                return

            track = item.get('track')
            if not track:
                return

            # Validate required fields
            if not track.get('artists') or not track.get('album') or not track.get('id'):
                return

            artist_name = track['artists'][0].get('name') or 'Unknown Artist'
            track_name = track.get('name') or 'Unknown Track'
            track_id = track['id']

            dedup_key = self.create_dedup_key(artist_name, track_name)

            # Skip if already counted in this playlist
            if dedup_key in playlist_counted_tracks_global[playlist_id]:
                return

            playlist_counted_tracks_global[playlist_id].add(dedup_key)

            release_date = track['album'].get('release_date', '')
            release_year = release_date[:4] if release_date and len(release_date) >= 4 else '0'

            print(f"Processing {playlist_name}: {artist_name} - {track_name} ({which}/{total})")

            # Store track info in playlist progress
            track_info = {
                'artist': artist_name,
                'track_name': track_name,
                'release_year': release_year,
                'release_date': release_date,
                'track_id': track_id
            }
            playlist_progress[playlist_id]['track_info'].append(track_info)

            # Update global track data
            if dedup_key not in track_data:
                track_data[dedup_key] = {
                    'artist': artist_name,
                    'track_name': track_name,
                    'normalized_track_name': self.normalize_track_name(track_name),
                    'track_ids': {},
                    'release_years': {},
                    'release_dates': {},
                    'count': 0
                }

            # Update track statistics
            track_data[dedup_key]['track_ids'][track_id] = (
                track_data[dedup_key]['track_ids'].get(track_id, 0) + 1
            )
            track_data[dedup_key]['release_years'][release_year] = (
                track_data[dedup_key]['release_years'].get(release_year, 0) + 1
            )
            track_data[dedup_key]['release_dates'][release_date] = (
                track_data[dedup_key]['release_dates'].get(release_date, 0) + 1
            )
            track_data[dedup_key]['count'] += 1

            # Add to all_tracks if within year range
            try:
                year_int = int(release_year) if release_year != '0' else 0
                if ((start_year is None or start_year <= year_int) and
                    (end_year is None or year_int <= end_year)):
                    all_tracks.add(dedup_key)
            except (ValueError, TypeError):
                pass

        except (UnicodeEncodeError, KeyError, IndexError, TypeError) as e:
            print(f"Track processing error: {str(e)}")

    @staticmethod
    def get_most_common(data_dict: dict) -> Optional[str]:
        """Get the most common value from a frequency dictionary"""
        if not data_dict:
            return None
        return max(data_dict.items(), key=lambda x: x[1])[0]

    def create_playlist(self, track_data_all_queries: List[dict], query_list: List[str],
                       threshold: int, start_year: Optional[int], end_year: Optional[int]):
        """Create a Spotify playlist from collected track data"""

        # Combine track data from all queries
        combined_track_data = self._combine_track_data(track_data_all_queries)

        # Filter by threshold
        filtered_tracks = {
            dedup_key: data
            for dedup_key, data in combined_track_data.items()
            if data['count'] >= threshold
        }

        if not filtered_tracks:
            print(f"No tracks meet the count threshold ({threshold} or more) to create a playlist.")
            return

        # Randomize then sort by count
        tracks_list = list(filtered_tracks.items())
        random.shuffle(tracks_list)
        sorted_tracks = sorted(tracks_list, key=lambda x: x[1]['count'], reverse=True)

        # Create playlist
        playlist_name = f"generated: {', '.join(query_list)}"
        if start_year is not None and end_year is not None:
            playlist_name += f" [{start_year}-{end_year}]"

        try:
            new_playlist = self.safe_api_call(
                self.sp.user_playlist_create,
                self.user_id,
                playlist_name,
                public=False
            )
            new_playlist_id = new_playlist['id']
        except (requests.exceptions.HTTPError, KeyError) as e:
            print(f"Failed to create playlist: {str(e)}")
            return

        # Add tracks to playlist
        self._add_tracks_to_playlist(
            new_playlist_id, sorted_tracks, playlist_name,
            start_year, end_year, filtered_tracks
        )

        try:
            print(f"Playlist created: {new_playlist['external_urls']['spotify']}")
        except KeyError:
            print(f"Playlist created with ID: {new_playlist_id}")

    def _combine_track_data(self, track_data_all_queries: List[dict]) -> dict:
        """Combine track data from multiple queries"""
        combined = {}

        for track_data_query in track_data_all_queries:
            if not track_data_query:
                continue

            for dedup_key, data in track_data_query.items():
                if dedup_key not in combined:
                    combined[dedup_key] = {
                        'artist': data['artist'],
                        'track_name': data['track_name'],
                        'normalized_track_name': data.get('normalized_track_name', ''),
                        'count': data['count'],
                        'track_ids': data.get('track_ids', {}).copy(),
                        'release_years': data.get('release_years', {}).copy(),
                        'release_dates': data.get('release_dates', {}).copy()
                    }
                else:
                    combined[dedup_key]['count'] += data['count']

                    for track_id, count in data.get('track_ids', {}).items():
                        combined[dedup_key]['track_ids'][track_id] = (
                            combined[dedup_key]['track_ids'].get(track_id, 0) + count
                        )

                    for release_year, count in data.get('release_years', {}).items():
                        combined[dedup_key]['release_years'][release_year] = (
                            combined[dedup_key]['release_years'].get(release_year, 0) + count
                        )

                    for release_date, count in data.get('release_dates', {}).items():
                        combined[dedup_key]['release_dates'][release_date] = (
                            combined[dedup_key]['release_dates'].get(release_date, 0) + count
                        )

        return combined

    def _add_tracks_to_playlist(self, playlist_id: str, sorted_tracks: List[tuple],
                                playlist_name: str, start_year: Optional[int],
                                end_year: Optional[int], filtered_tracks: dict):
        """Add tracks to the created playlist in batches"""
        added_dedup_keys = set()
        tracks_to_add = []

        for dedup_key, data in sorted_tracks:
            try:
                release_year_str = self.get_most_common(data.get('release_years', {})) or '0'
                try:
                    release_year = int(release_year_str) if release_year_str != '0' else 0
                except (ValueError, TypeError):
                    continue

                # Check year range
                if ((start_year is None or start_year <= release_year) and
                    (end_year is None or release_year <= end_year)):

                    if dedup_key not in added_dedup_keys:
                        added_dedup_keys.add(dedup_key)
                        track_id = self.get_most_common(data.get('track_ids', {}))

                        if track_id:
                            tracks_to_add.append(track_id)
                            track_id_count = data['track_ids'].get(track_id, 0)
                            release_year_count = data['release_years'].get(release_year_str, 0)

                            print(f"'{playlist_name}': {data['artist']} - {data['track_name']} "
                                  f"({release_year_str}) ({len(added_dedup_keys)}/{len(filtered_tracks)}) - "
                                  f"Total count: {data['count']} (track_id: {track_id_count}x, "
                                  f"year: {release_year_count}x)")

                            # Add batch when full
                            if len(tracks_to_add) == BATCH_SIZE:
                                try:
                                    self.safe_api_call(
                                        self.sp.playlist_add_items,
                                        playlist_id,
                                        tracks_to_add
                                    )
                                except requests.exceptions.HTTPError as e:
                                    print(f"Failed to add batch of tracks: {str(e)}")
                                tracks_to_add = []

            except (KeyError, ValueError, TypeError) as e:
                print(f"Error processing track: {str(e)}")
                continue

        # Add remaining tracks
        if tracks_to_add:
            try:
                self.safe_api_call(self.sp.playlist_add_items, playlist_id, tracks_to_add)
                print(f"Added a batch of {len(tracks_to_add)} tracks to the playlist")
            except requests.exceptions.HTTPError as e:
                print(f"Failed to add final batch of tracks: {str(e)}")

    def cleanup(self):
        """Clean up cache files"""
        if os.path.exists(self.cache_file):
            try:
                # Only remove if it's the standard Spotipy cache
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    if 'access_token' in data:  # Verify it's a Spotipy cache
                        os.remove(self.cache_file)
            except (json.JSONDecodeError, IOError, OSError):
                pass


def get_user_queries() -> List[str]:
    """Get search queries from user input"""
    while True:
        queries = input("Enter sentences separated by a comma (e.g., 'Pink Floyd music, Beatles songs'): ").strip()
        if queries:
            query_list = [query.strip().lower() for query in queries.split(',') if query.strip()]
            if query_list:
                return query_list
            print("Error: Please enter at least one valid query.")
        else:
            print("Error: Input cannot be empty.")


def get_release_year_range() -> Tuple[Optional[int], Optional[int]]:
    """Get release year range from user input"""
    while True:
        release_year_range = input(
            "Enter the release year range (e.g., 1915-2018), or press Enter to skip: "
        ).strip()

        if not release_year_range:
            return None, None

        try:
            if '-' not in release_year_range:
                print("Error: Please use format 'YYYY-YYYY' (e.g., 1915-2018)")
                continue

            parts = release_year_range.split('-')
            if len(parts) != 2:
                print("Error: Please use format 'YYYY-YYYY' (e.g., 1915-2018)")
                continue

            start_year, end_year = int(parts[0]), int(parts[1])

            if start_year < 1000 or start_year > 9999 or end_year < 1000 or end_year > 9999:
                print("Error: Years must be 4-digit numbers")
                continue

            if start_year > end_year:
                print("Error: Start year must be before or equal to end year")
                continue

            return start_year, end_year
        except ValueError:
            print("Error: Please enter valid numbers in format 'YYYY-YYYY'")


def get_threshold() -> int:
    """Get threshold value from user input"""
    threshold_input = input(
        "Enter the threshold value for track count (press Enter for default 3): "
    ).strip()

    if threshold_input:
        try:
            threshold = int(threshold_input)
            if threshold < 1:
                print("Threshold must be at least 1. Using default value of 3.")
                return 3
            return threshold
        except ValueError:
            print("Invalid threshold value. Using default value of 3.")
            return 3
    return 3


def main():
    """Main execution function"""
    analyzer = SpotifyPlaylistAnalyzer(client_id, client_secret)

    try:
        analyzer.initialize()

        queries = get_user_queries()
        threshold = get_threshold()
        start_year, end_year = get_release_year_range()

        processed_playlists_global, playlist_counted_tracks_global = (
            analyzer.load_global_processed_playlists()
        )

        print(f"\n📂 Loaded {len(processed_playlists_global)} globally processed playlists")
        print(f"📂 Loaded counting data for {len(playlist_counted_tracks_global)} playlists")

        track_data_all_queries = []
        new_playlists_completed = 0
        new_playlists_skipped = 0

        for query in queries:
            stats = process_query(
                analyzer, query, start_year, end_year,
                processed_playlists_global, playlist_counted_tracks_global
            )

            track_data_all_queries.append(stats['track_data'])
            new_playlists_completed += stats['completed']
            new_playlists_skipped += stats['skipped']

        print(f"\n{'='*60}")
        print(f"📊 GLOBAL STATISTICS")
        print(f"{'='*60}")
        print(f"Total unique playlists checked (all time): {len(processed_playlists_global)}")
        print(f"\nThis session:")
        print(f"  - Playlists completed (matching queries): {new_playlists_completed}")
        print(f"  - Playlists skipped (not matching): {new_playlists_skipped}")
        print(f"  - Total processed this session: {new_playlists_completed + new_playlists_skipped}")
        print(f"{'='*60}\n")

        # Print summary statistics
        print_track_statistics(track_data_all_queries, analyzer)

        # Create final playlist
        analyzer.create_playlist(track_data_all_queries, queries, threshold, start_year, end_year)

    except ValueError as e:
        print(f"Configuration error: {str(e)}")
        import traceback
        traceback.print_exc()
    except requests.exceptions.RequestException as e:
        print(f"Network error: {str(e)}")
        import traceback
        traceback.print_exc()
    except KeyboardInterrupt:
        print("\n\nScript interrupted by user. Progress has been saved.")
    except Exception as e:
        print(f"An unexpected error occurred: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        analyzer.cleanup()


def process_query(analyzer: SpotifyPlaylistAnalyzer, query: str,
                 start_year: Optional[int], end_year: Optional[int],
                 processed_playlists_global: Set[str],
                 playlist_counted_tracks_global: Dict[str, Set[str]]) -> dict:
    """Process a single query and return statistics"""

    statefile = f'progress_{analyzer.user_id}_{query}.json'
    current_state = analyzer.load_progress(statefile)

    if current_state.get('processed_playlists'):
        processed_playlists_global.update(current_state['processed_playlists'])

    new_playlists_this_query = 0
    new_playlists_skipped = 0  # FIXED: Added counter for skipped playlists
    main_retry_counter = 0

    while True:
        if new_playlists_this_query >= MAX_PLAYLISTS_PER_QUERY:
            print(f"Max NEW playlists for query '{query}' reached ({MAX_PLAYLISTS_PER_QUERY}).")
            break

        current_offset = current_state['search_offset']
        if current_offset + SEARCH_LIMIT > MAX_SPOTIFY_OFFSET:
            print(f"Reached Spotify API offset limit ({MAX_SPOTIFY_OFFSET}) for query '{query}'.")
            break

        try:
            results = analyzer.safe_api_call(
                analyzer.sp.search,
                f'*{query}*',
                limit=SEARCH_LIMIT,
                offset=current_offset,
                type='playlist'
            )

            if not results or 'playlists' not in results:
                print("No results returned from search")
                break

            playlist = results['playlists']
            if not playlist or 'items' not in playlist:
                print("No playlists found")
                break

            items = playlist['items']
            if not items:
                print(f"No more playlists found for query '{query}'")
                break

            total = playlist.get('total', 0)
            matching_playlists_count = sum(
                1 for p, progress in current_state['playlist_progress'].items()
                if progress.get('completed', False) and progress.get('matched_query', True)
            )

            for item in items:
                if not item or 'owner' not in item or 'id' not in item:
                    continue

                if item['owner']['id'] != analyzer.user_id:
                    playlist_id = item['id']

                    if playlist_id not in processed_playlists_global:
                        playlist_display_name = item.get('name', 'Unknown')
                        print(f"\n🎵 Checking playlist '{playlist_display_name}' "
                              f"({matching_playlists_count + 1} matching playlists for '{query}')")

                        status = analyzer.process_playlist(
                            matching_playlists_count + 1, total, item,
                            current_state['all_tracks'], current_state['playlist_progress'],
                            current_state['track_data'], query, start_year, end_year,
                            playlist_counted_tracks_global, processed_playlists_global
                        )

                        analyzer.save_all_state(
                            query, current_state,
                            processed_playlists_global, playlist_counted_tracks_global
                        )

                        if status == 'completed':
                            current_state['processed_playlists'].append(playlist_id)
                            processed_playlists_global.add(playlist_id)
                            new_playlists_this_query += 1
                            matching_playlists_count += 1
                            print(f"✅ Playlist completed (#{new_playlists_this_query} "
                                  f"matching query '{query}')")
                        elif status == 'skipped':
                            current_state['processed_playlists'].append(playlist_id)
                            processed_playlists_global.add(playlist_id)
                            new_playlists_skipped += 1  # FIXED: Now properly incremented
                            print(f"⏭️  Playlist skipped (doesn't match query)")
                        elif status == 'incomplete':
                            print(f"⚠️  Playlist incomplete, will resume on next run")

                        analyzer.save_all_state(
                            query, current_state,
                            processed_playlists_global, playlist_counted_tracks_global
                        )
                    else:
                        print(f"⏭️  Skipping playlist '{item.get('name', 'Unknown')}' "
                              f"(already processed)")

            if playlist and playlist.get('next'):
                current_state['search_offset'] += SEARCH_LIMIT
                analyzer.save_progress(current_state, statefile)
            else:
                break

        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            main_retry_counter += 1
            print(f"{type(e).__name__}. Retrying... ({main_retry_counter}/{MAIN_MAX_RETRIES})")
            time.sleep(5)
            if main_retry_counter >= MAIN_MAX_RETRIES:
                print("Max retries reached in main search loop.")
                break
            continue
        except requests.exceptions.HTTPError as e:
            action = analyzer.handle_http_error(e, "search")
            if action == 'retry':
                continue
            elif action == 'auth_error':
                print("Authentication failed during search. Exiting.")
                raise
            else:
                break
        except (ValueError, KeyError) as e:
            print(f"Error during search: {str(e)}")
            break

    print(f"\n✅ Query '{query}' complete:")
    print(f"   - Playlists matching query processed: {new_playlists_this_query}")
    print(f"   - Total playlists in query file: {len(current_state['processed_playlists'])}")
    print(f"   - Unique tracks found: {len(current_state['track_data'])}")

    return {
        'track_data': current_state['track_data'],
        'completed': new_playlists_this_query,
        'skipped': new_playlists_skipped  # FIXED: Now returns actual count
    }


def print_track_statistics(track_data_all_queries: List[dict],
                           analyzer: SpotifyPlaylistAnalyzer):
    """Print statistics about all collected tracks"""
    all_tracks_counts = {}

    for track_data_query in track_data_all_queries:
        if not track_data_query:
            continue

        for dedup_key, data in track_data_query.items():
            if dedup_key not in all_tracks_counts:
                all_tracks_counts[dedup_key] = {
                    'artist': data['artist'],
                    'track_name': data['track_name'],
                    'count': data['count'],
                    'track_ids': data.get('track_ids', {}).copy(),
                    'release_years': data.get('release_years', {}).copy(),
                    'release_dates': data.get('release_dates', {}).copy()
                }
            else:
                all_tracks_counts[dedup_key]['count'] += data['count']

                for track_id, count in data.get('track_ids', {}).items():
                    all_tracks_counts[dedup_key]['track_ids'][track_id] = (
                        all_tracks_counts[dedup_key]['track_ids'].get(track_id, 0) + count
                    )

                for release_year, count in data.get('release_years', {}).items():
                    all_tracks_counts[dedup_key]['release_years'][release_year] = (
                        all_tracks_counts[dedup_key]['release_years'].get(release_year, 0) + count
                    )

                for release_date, count in data.get('release_dates', {}).items():
                    all_tracks_counts[dedup_key]['release_dates'][release_date] = (
                        all_tracks_counts[dedup_key]['release_dates'].get(release_date, 0) + count
                    )

    for dedup_key, data in all_tracks_counts.items():
        most_common_track_id = analyzer.get_most_common(data.get('track_ids', {}))
        most_common_year = analyzer.get_most_common(data.get('release_years', {})) or '0'
        track_id_count = data['track_ids'].get(most_common_track_id, 0) if most_common_track_id else 0
        year_count = data['release_years'].get(most_common_year, 0)

        print(f"{data['artist']} - {data['track_name']}: {data['count']} occurrences "
              f"(most common year: {most_common_year} [{year_count}x], track_id: {track_id_count}x)")


if __name__ == "__main__":
    main()

import spotipy
from spotipy.oauth2 import SpotifyOAuth
import json
import requests
import csv
import re
import os
import threading
import time
track_counts = {}

# Load client IDs and secrets from ids.csv
client_credentials = []

with open("ids.csv", "r", encoding="utf-8") as csvfile:
    reader = csv.DictReader(csvfile)
    for row in reader:
        client_credentials.append({
            'client_id': row["client_id"],
            'client_secret': row["client_secret"]
        })

# Function to get a list of queries from the user and convert them to lowercase
def get_user_queries():
    queries = input("Enter sentences separated by a comma (e.g., 'Pink Floyd music, Beatles songs'): ")
    query_list = [query.strip().lower() for query in queries.split(',')]
    return query_list

# Function to process playlists
def process_playlist(which, total, item, all_tracks, playlist_progress, track_counts, user_id, query):
    playlist_id = item['id']
    playlist_name = item['name']
    if playlist_id not in playlist_progress:
        playlist_progress[playlist_id] = {'offset': 0}

    # Check if the query is an exact word match in the playlist name, case-insensitive
    if re.search(rf'\b{re.escape(query.lower())}\b', playlist_name.lower(), re.IGNORECASE):
        while True:
            offset = playlist_progress[playlist_id]['offset']
            try:
                tracks = sp.playlist_tracks(playlist_id, offset=offset)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 400 and "Error parsing JSON" in str(e):
                    print(f"Error parsing JSON for {playlist_name}, skipping this playlist.")
                    break  # Skip this playlist and continue with the next one
                else:
                    raise  # Re-raise the exception if it's not the expected error

            for item in tracks['items']:
                track = item['track']
                if track:
                    artist_name = track['artists'][0]['name']
                    track_name = track['name']
                    query = f'artist:{artist_name} track:{track_name.split(" - ")[0]}'
                    print(f"Processing {playlist_name}: {artist_name} - {track_name} ({which}/{total})")
                    if query not in all_tracks:
                        all_tracks.add(query) if isinstance(all_tracks, set) else all_tracks.append(query)
                    track_counts[query] = track_counts.get(query, 0) + 1

            if tracks['next']:
                playlist_progress[playlist_id]['offset'] += len(tracks['items'])
            else:
                break
# Introduce a time delay to avoid hitting API limits
            time.sleep(1)  # Sleep for 1 second
        playlist_progress[playlist_id]['offset'] = 0


# Function to save progress
def save_progress(data, filename):
    if 'all_tracks' in data:
        data['all_tracks'] = list(data['all_tracks'])
    else:
        data['all_tracks'] = []
    with open(filename, 'w', encoding='utf-8') as f:  # Specify utf-8 encoding
        json.dump(data, f, ensure_ascii=False, indent=4)  # Set ensure_ascii to False to handle non-ASCII characters


# Function to load progress
def load_progress(filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f:  # Specify utf-8 encoding
            data = json.load(f)
            if 'all_tracks' in data and isinstance(data['all_tracks'], list):
                data['all_tracks'] = set(data['all_tracks'])
            if 'which' not in data:
                data['which'] = 0
            if 'processed_playlists' not in data:
                data['processed_playlists'] = []
            if 'playlist_progress' not in data:
                data['playlist_progress'] = {}
            return data
    except FileNotFoundError:
        return {
            'which': 0,
            'processed_playlists': [],
            'all_tracks': set(),
            'playlist_progress': {},
        }


# Modify the create_playlist function to use batch creation
def create_playlist(track_counts_all_queries, query_list, threshold):
    combined_track_counts = {}
    all_tracks = set()  # Store all tracks from all queries

    # Accumulate all tracks from all queries
    for track_counts_this_query in track_counts_all_queries:
        for query, count in track_counts_this_query.items():
            combined_track_counts[query] = combined_track_counts.get(query, 0) + count
            all_tracks.add(query)

    # Apply the threshold to all tracks
    filtered_tracks = {query: count for query, count in combined_track_counts.items() if count >= threshold}

    sorted_tracks = sorted(filtered_tracks.keys(), key=lambda x: filtered_tracks[x], reverse=True)

    if sorted_tracks:
        user_id = sp.current_user()['id']
        playlist_name = "generated: " + ", ".join(query_list)
        new_playlist = sp.user_playlist_create(user_id, playlist_name, public=False)
        new_playlist_id = new_playlist['id']

        added_track_ids = set()
        batch_size = 100

        for i in range(0, len(sorted_tracks), batch_size):
            batch_queries = sorted_tracks[i:i + batch_size]

            tracks_to_add = []
            for query in batch_queries:
                track_name = query.split(" track:")[1]
                artist_name = query.split(" track:")[0].split("artist:")[1]
                new_query = f'artist:{artist_name} track:{track_name.split(" - ")[0]}'

                search_results = sp.search(q=new_query, type='track', limit=1)
                track_items = search_results['tracks']['items']

                if track_items:
                    track = track_items[0]
                    track_id = track['id']

                    if track_id not in added_track_ids:
                        added_track_ids.add(track_id)
                        tracks_to_add.append(track_id)

                        print(f"Added track to the playlist: {artist_name} - {track_name} ({len(added_track_ids)}/{len(sorted_tracks)})")

            if tracks_to_add:
                sp.playlist_add_items(new_playlist_id, tracks_to_add)

            print(f"Batch {i // batch_size + 1}/{len(sorted_tracks) // batch_size + 1} added to the playlist")

        print(f"Playlist created: {new_playlist['external_urls']['spotify']}")
    else:
        print(f"No tracks meet the count threshold ({threshold} or more) to create a playlist.")









# Start the main program
if __name__ == "__main__":
    try:
        queries = get_user_queries()
        threshold = 3  # Set your desired threshold here
        track_counts_all_queries = []

        # Initialize track counts list
        sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=client_credentials[0]['client_id'],
                                                       client_secret=client_credentials[0]['client_secret'],
                                                       redirect_uri="http://localhost:8000/callback/",
                                                       scope="playlist-modify-private"))
        for query in queries:
            current_state = load_progress(f'progress_{sp.current_user()["id"]}_{query}.json')

            if current_state and current_state.get('which', 0) > 0:
                # Filter tracks based on the threshold for JSON progress
                track_counts_all_queries.append({query: count for query, count in current_state['track_counts'].items() if count >= threshold})
            else:
                current_state = {
                    'which': 0,
                    'processed_playlists': [],
                    'all_tracks': set(),
                    'playlist_progress': {},
                    'track_counts': {}
                }

            playlist_id = None
            limit = 50

            max_playlists_per_query = 100000 // len(queries)  # Divide max playlists by the number of queries
            unique_processed_playlists_count = 0

            while True:
                if unique_processed_playlists_count >= max_playlists_per_query:
                    break  # Break the loop if the max playlists per query are processed

                if len(current_state['processed_playlists']) >= max_playlists_per_query:
                    print(f"Max playlists per query reached ({max_playlists_per_query}). Skipping the query.")
                    break  # Skip the query

                try:
                    results = sp.search(f'*{query}*', limit=limit, offset=current_state['playlist_progress'].get(playlist_id, {'offset': 0})['offset'], type='playlist')

                    playlist = results['playlists']
                    total = playlist['total']
                    for item in playlist['items']:
                        if item['owner']['id'] != sp.current_user()['id']:
                            playlist_id = item['id']
                            if playlist_id not in current_state['processed_playlists']:
                                process_playlist(len(current_state['processed_playlists']) + 1, total, item, current_state['all_tracks'], current_state['playlist_progress'], current_state['track_counts'], sp.current_user()["id"], query)
                                current_state['processed_playlists'].append(playlist_id)
                                unique_processed_playlists_count += 1
                                save_progress(current_state, f'progress_{sp.current_user()["id"]}_{query}.json')
                            current_state['which'] += 1

                    if playlist['next']:
                        current_state['playlist_progress'][playlist_id]['offset'] += limit
                        save_progress(current_state, f'progress_{sp.current_user()["id"]}_{query}.json')
                    else:
                        break
                except requests.exceptions.ReadTimeout:
                    print("Read timed out. Retrying...")
                    continue
                except spotipy.exceptions.SpotifyException as e:
                    if "HTTP Error 413" in str(e):
                        print("HTTP Error 413 occurred. Exiting...")
                        break
                    elif e.http_status == 400 or e.http_status == 403:
                        print(f"HTTP Error {e.http_status} occurred. Skipping the query.")
                        break
                    elif e.http_status == 404:
                        print(f"HTTP Error {e.http_status} occurred. Playlist not found. Skipping the query.")
                        break
                    elif e.http_status == 429 or e.http_status == 500:
                        print(f"HTTP Error {e.http_status} occurred. Waiting and retrying.")
                        time.sleep(60)  # Sleep for 60 seconds before retrying
                    else:
                        print(f"HTTP Error {e.http_status} occurred. Exiting...")
                        break

            track_counts_all_queries.append(current_state['track_counts'])

        # Count all the tracks from the queries
        all_tracks_counts = {}
        for track_counts_query in track_counts_all_queries:
            for query, count in track_counts_query.items():
                all_tracks_counts[query] = all_tracks_counts.get(query, 0) + count

        # Print the counts
        for query, count in all_tracks_counts.items():
            print(f"{query}: {count} tracks")

        create_playlist(track_counts_all_queries, queries, threshold)

    except Exception as e:
        print(f"An error occurred: {str(e)}")
    finally:
        if os.path.exists('.cache'):
            os.remove('.cache')

# SpotKnot - Spotify Playlist Miner

## Overview

This Python script uses the Spotify API to generate playlists based on user-defined queries. It searches for playlists matching the given queries, processes the tracks within those playlists, and creates a new playlist with a curated selection of tracks that meet specified criteria. 

### Requirements

Make sure you have the following dependencies installed:

```bash
pip install spotipy requests==2.26.0 && pip install json csv re threading time random os tempfile shutil
```

### Getting Started 

Obtain Spotify API credentials:

1. Create a Spotify Developer account and create a new application to get your `client_id` and `client_secret`.

2. Update the `ids.csv` file with your credentials.

Run the script: 

```bash
python SpotKnot.py
```

### Usage

1. Enter sentences separated by a comma when prompted (e.g., 'Pink Floyd music, Beatles songs').

2. Enter the release year range (e.g., 1915-2018) or press Enter to skip. 

3. Enter the threshold value for track count (press Enter for default 3).

The script will then search for playlists matching the given queries, process the tracks, and generate a new playlist with tracks that meet the specified criteria.

### Notes

- The script saves progress to JSON files, allowing you to resume or review the playlist generation process.

- To avoid API limits, the script includes time delays between requests.

### Disclaimer

This script is provided as-is and may be subject to changes in the Spotify API or its usage policies. Use it responsibly and ensure compliance with Spotify's terms of service.

Happy listening! 🎶

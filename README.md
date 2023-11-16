SpotKnot: Spotify Playlist Generator

SpotKnot is a Python script that enables you to generate personalized Spotify playlists based on your favorite artists and tracks. Using the Spotipy library to interact with the Spotify API, SpotKnot allows you to create playlists tailored to your specific queries.
Prerequisites

Before using SpotKnot, ensure that you have the required dependencies installed. You can install them using the following command:

bash

pip install spotipy requests

Setup

    Clone the SpotKnot repository to your local machine.
    Create a Spotify Developer account and register your application to obtain the necessary client IDs and secrets.
    Save your client IDs and secrets in the ids.csv file.
    Run SpotKnot using the following command:

bash

python spotknot.py

Usage

    When prompted, enter sentences separated by commas (e.g., 'Pink Floyd music, Beatles songs').
    Set your desired threshold for the number of tracks to include in the playlist.

Requirements

Ensure that you have the following Python libraries installed:

    spotipy
    requests

You can install these dependencies using the following command:

bash

pip install spotipy requests

Features

    Query Matching: SpotKnot searches for playlists containing your specified keywords in their names.
    Playlist Processing: Retrieve tracks from matched playlists, considering your defined threshold.
    Playlist Creation: Generate a new playlist with the accumulated tracks that meet the threshold.

Notes

    The script introduces a time delay to avoid hitting Spotify API limits.
    Progress is saved and loaded to/from JSON files, enabling you to resume the process.
    Modify the create_playlist function to customize playlist creation based on your preferences.

Feel free to explore and customize SpotKnot to suit your music preferences!

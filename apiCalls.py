import os
import requests

DEFAULT_PATH = os.getenv("DEFAULT_API_PATH")


def get_live_matches():
    path = "match?q=live_score"
    return requests.get(DEFAULT_PATH + path)


def get_upcoming():
    path = "match?q=upcoming"
    return requests.get(DEFAULT_PATH + path)


def get_health():
    path = "health"
    return requests.get(DEFAULT_PATH + path)
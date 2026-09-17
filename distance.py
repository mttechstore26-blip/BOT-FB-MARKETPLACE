import json
import urllib.parse
import urllib.request
from functools import lru_cache

from geopy.geocoders import Nominatim
from geopy.distance import geodesic


HOME_LOCATION = "89015 Palmi, Reggio Calabria, Italia"

geolocator = Nominatim(
    user_agent="mt-tech-facebook-marketplace-monitor"
)


@lru_cache(maxsize=512)
def geocode_location(location):
    if not location:
        return None

    try:
        result = geolocator.geocode(
            f"{location}, Italia",
            timeout=10,
            country_codes="it"
        )

        if not result:
            return None

        return (
            result.latitude,
            result.longitude
        )

    except Exception:
        return None


@lru_cache(maxsize=1)
def get_home_coordinates():
    try:
        result = geolocator.geocode(
            HOME_LOCATION,
            timeout=10,
            country_codes="it"
        )

        if not result:
            return None

        return (
            result.latitude,
            result.longitude
        )

    except Exception:
        return None


def road_distance(home, destination):
    """
    Distanza stradale tramite OSRM.
    Coordinate:
    (latitudine, longitudine)
    """

    try:
        home_lat, home_lon = home
        dest_lat, dest_lon = destination

        url = (
            "https://router.project-osrm.org/route/v1/driving/"
            f"{home_lon},{home_lat};"
            f"{dest_lon},{dest_lat}"
            "?overview=false"
        )

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                "MT-TECH-Marketplace-Monitor/1.0"
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            data = json.loads(
                response.read().decode("utf-8")
            )

        routes = data.get("routes", [])

        if not routes:
            return None

        meters = routes[0]["distance"]

        return round(meters / 1000)

    except Exception:
        return None


def distance_from_home(location):
    home = get_home_coordinates()
    destination = geocode_location(location)

    if not home or not destination:
        return None

    # Prima prova distanza stradale
    km = road_distance(
        home,
        destination
    )

    if km is not None:
        return km

    # Fallback: linea d'aria se il routing non funziona
    return round(
        geodesic(
            home,
            destination
        ).km
    )

import json
import math
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings


class RouteServiceError(Exception):
    def __init__(self, message, status=502):
        self.status = status
        super().__init__(message)


def calculate_osrm_route(start_lat, start_lng, destination_lat, destination_lng, referer=''):
    base_url = settings.OSRM_BASE_URL
    if not base_url.startswith(('https://', 'http://')):
        raise RouteServiceError('Routing service configuration is invalid.', status=503)

    coordinates = f'{start_lng:.7f},{start_lat:.7f};{destination_lng:.7f},{destination_lat:.7f}'
    query = urlencode({'overview': 'full', 'geometries': 'geojson', 'alternatives': 'false', 'steps': 'false'})
    route_url = f'{base_url}/route/v1/driving/{coordinates}?{query}'
    headers = {'Accept': 'application/json', 'User-Agent': 'PackTrace/1.0'}
    if referer:
        headers['Referer'] = referer
    request = Request(route_url, headers=headers)

    try:
        with urlopen(request, timeout=8) as response:
            raw_response = response.read(5_000_001)
    except TimeoutError as error:
        raise RouteServiceError('The routing service timed out.', status=504) from error
    except HTTPError as error:
        raise RouteServiceError('The routing service returned an error.', status=502) from error
    except (URLError, OSError, HTTPException) as error:
        raise RouteServiceError('The routing service is unavailable.', status=502) from error

    if len(raw_response) > 5_000_000:
        raise RouteServiceError('The routing service response was too large.')
    try:
        payload = json.loads(raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RouteServiceError('The routing service returned invalid data.') from error

    routes = payload.get('routes') if isinstance(payload, dict) else None
    route = routes[0] if isinstance(payload, dict) and payload.get('code') == 'Ok' and isinstance(routes, list) and routes else None
    geometry = route.get('geometry') if isinstance(route, dict) else None
    raw_coordinates = geometry.get('coordinates') if isinstance(geometry, dict) else None
    if not isinstance(raw_coordinates, list) or not 2 <= len(raw_coordinates) <= 100_000:
        raise RouteServiceError('No valid road route was found.', status=422)

    route_coordinates = []
    for coordinate in raw_coordinates:
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            raise RouteServiceError('The routing service returned invalid geometry.')
        try:
            longitude, latitude = float(coordinate[0]), float(coordinate[1])
        except (TypeError, ValueError, OverflowError) as error:
            raise RouteServiceError('The routing service returned invalid geometry.') from error
        if (not math.isfinite(latitude) or not math.isfinite(longitude)
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            raise RouteServiceError('The routing service returned invalid geometry.')
        route_coordinates.append([longitude, latitude])

    try:
        distance_meters = float(route['distance'])
        duration_seconds = float(route['duration'])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise RouteServiceError('The routing service returned incomplete route metrics.') from error
    if (not math.isfinite(distance_meters) or not math.isfinite(duration_seconds)
            or distance_meters <= 0 or duration_seconds <= 0):
        raise RouteServiceError('The routing service returned invalid route metrics.')

    return {
        'success': True,
        'distance_meters': distance_meters,
        'duration_seconds': duration_seconds,
        'route_coordinates': route_coordinates,
    }
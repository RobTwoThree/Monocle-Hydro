from argparse import ArgumentParser
from datetime import datetime
from multiprocessing.managers import BaseManager, RemoteError
from time import time

from monocle import sanitized as conf
from monocle.db import get_forts, get_pokestops, get_darkstops, get_grouped_quest_task, Pokestop, session_scope, Sighting, Spawnpoint, Raid, Fort, FortSighting
from monocle.weather import Weather
from monocle.utils import Units, get_address, dump_pickle, load_pickle
from monocle.names import DAMAGE, MOVES, POKEMON
from monocle.bounds import north, south, east, west

import s2sphere
import overpy
from shapely.geometry import Polygon, Point, LineString

if conf.MAP_WORKERS:
    try:
        UNIT = getattr(Units, conf.SPEED_UNIT.lower())
        if UNIT is Units.miles:
            UNIT_STRING = "MPH"
        elif UNIT is Units.kilometers:
            UNIT_STRING = "KMH"
        elif UNIT is Units.meters:
            UNIT_STRING = "m/h"
    except AttributeError:
        UNIT_STRING = "MPH"

def get_args():
    parser = ArgumentParser()
    parser.add_argument(
        '-H',
        '--host',
        help='Set web server listening host',
        default='0.0.0.0'
    )
    parser.add_argument(
        '-P',
        '--port',
        type=int,
        help='Set web server listening port',
        default=5000
    )
    parser.add_argument(
        '-d', '--debug', help='Debug Mode', action='store_true'
    )
    parser.set_defaults(debug=False)
    return parser.parse_args()


class AccountManager(BaseManager): pass
AccountManager.register('worker_dict')


class Workers:
    def __init__(self):
        self._data = {}
        self._manager = AccountManager(address=get_address(), authkey=conf.AUTHKEY)

    def connect(self):
        try:
            self._manager.connect()
            self._data = self._manager.worker_dict()
        except (FileNotFoundError, AttributeError, RemoteError, ConnectionRefusedError, BrokenPipeError):
            print('Unable to connect to manager for worker data.')
            self._data = {}

    @property
    def data(self):
        try:
            if self._data:
                return self._data.items()
            else:
                raise ValueError
        except (FileNotFoundError, RemoteError, ConnectionRefusedError, ValueError, BrokenPipeError):
            self.connect()
            return self._data.items()


def get_worker_markers(workers):
    return [{
        'lat': lat,
        'lon': lon,
        'worker_no': worker_no,
        'time': datetime.fromtimestamp(timestamp).strftime('%I:%M:%S %p'),
        'speed': '{:.1f}{}'.format(speed, UNIT_STRING),
        'total_seen': total_seen,
        'visits': visits,
        'seen_here': seen_here
    } for worker_no, ((lat, lon), timestamp, speed, total_seen, visits, seen_here) in workers.data]


def sighting_to_marker(pokemon, names=POKEMON, moves=MOVES, damage=DAMAGE):
    pokemon_id = pokemon.pokemon_id
    marker = {
        'id': 'pokemon-' + str(pokemon.id),
        'trash': pokemon_id in conf.TRASH_IDS,
        'name': names[pokemon_id],
        'pokemon_id': pokemon_id,
        'lat': pokemon.lat,
        'lon': pokemon.lon,
        'expires_at': pokemon.expire_timestamp,
        'gender': pokemon.gender,
        'form': pokemon.form,
        'weather_boosted_condition': pokemon.weather_boosted_condition,
        'weather_cell_id': pokemon.weather_cell_id
    }
    move1 = pokemon.move_1
    if pokemon.move_1:
        move2 = pokemon.move_2
        marker['atk'] = pokemon.atk_iv
        marker['def'] = pokemon.def_iv
        marker['sta'] = pokemon.sta_iv
        marker['move1'] = moves[move1]
        marker['move2'] = moves[move2]
        marker['damage1'] = damage[move1]
        marker['damage2'] = damage[move2]
        marker['cp'] = pokemon.cp
        marker['level'] = pokemon.level
    if pokemon.form and pokemon.form > 0:
        marker['form'] = pokemon.form
    return marker


def get_pokemarkers(after_id=0):
    with session_scope() as session:
        pokemons = session.query(Sighting) \
            .filter(Sighting.expire_timestamp > time(),
                    Sighting.id > after_id)
        if conf.MAP_FILTER_IDS:
            pokemons = pokemons.filter(~Sighting.pokemon_id.in_(conf.MAP_FILTER_IDS))
        return tuple(map(sighting_to_marker, pokemons))


def get_raid_markers(names=POKEMON, moves=MOVES):
    with session_scope() as session:
        markers = []
        raids = session.query(Raid) \
            .filter(Raid.time_end > time())
        for raid in raids:
            fort = session.query(Fort) \
                .filter(Fort.id == raid.fort_id) \
                .scalar()
            fortsighting = session.query(FortSighting) \
                .filter(FortSighting.fort_id == fort.id) \
                .order_by(FortSighting.last_modified.desc()) \
                .first()
            markers.append({
                'id': 'raid-' + str(raid.id),
                'raid_id': raid.id,
                'fort_id': fortsighting.fort_id,
                'gym_name': fort.name,
                'image_url': fort.url,
                'external_id': fort.external_id,
                'gym_team': fortsighting.team,
                'raid_battle': raid.time_battle,
                'raid_spawn': raid.time_spawn,
                'raid_end': raid.time_end,
                'raid_level': raid.level,
                'raid_pokemon_id': raid.pokemon_id,
                'raid_pokemon_name': names[raid.pokemon_id],
                'raid_pokemon_cp': raid.cp,
                'raid_pokemon_form': raid.form,
                'raid_evolution': raid.evolution,
                'raid_pokemon_move_1': moves[raid.move_1],
                'raid_pokemon_move_2': moves[raid.move_2],
                'lat': fort.lat,
                'lon': fort.lon
                })

        return markers


def get_vertex(cell, v):
    vertex = s2sphere.LatLng.from_point(cell.get_vertex(v))

    return (vertex.lat().degrees, vertex.lng().degrees)

def get_weather():
    with session_scope() as session:
        weathers = session.query(Weather)
        markers = []
        for weather in weathers:
            cell = s2sphere.Cell(s2sphere.CellId(weather.s2_cell_id).parent(10))
            center = s2sphere.LatLng.from_point(cell.get_center())
            converted_s2_cell_id = s2sphere.CellId.from_lat_lng(s2sphere.LatLng.from_degrees(center.lat().degrees, center.lng().degrees)).parent(10)
            markers.append({
                'id': 'weather-' + str(weather.id),
                'coords': [(get_vertex(cell, v)) for v in range(0, 4)],
                'center': (center.lat().degrees, center.lng().degrees),
                'condition': weather.condition,
                'alert_severity': weather.alert_severity,
                'warn': weather.warn,
                'day': weather.day,
                's2_cell_id': converted_s2_cell_id.id()
            })
        return markers


def get_gym_markers(names=POKEMON):
    with session_scope() as session:
        forts = get_forts(session)
    return [{
        'id': 'fort-' + str(fort['fort_id']),
        'fort_id': fort['fort_id'],
        'sighting_id': fort['id'],
        'gym_name': fort['name'],
        'image_url': fort['url'],
        'external_id': fort['external_id'],
        'pokemon_id': fort['guard_pokemon_id'],
        'pokemon_name': names[fort['guard_pokemon_id']],
        'slots_available': fort['slots_available'],
        'time_occupied': fort['time_occupied'],
        'last_modified': fort['last_modified'],
        'team': fort['team'],
        'lat': fort['lat'],
        'lon': fort['lon'],
        'sponsor': fort['sponsor']
    } for fort in forts]


def get_spawnpoint_markers():
    with session_scope() as session:
        spawns = session.query(Spawnpoint)
        return [{
            'spawn_id': spawn.spawn_id,
            'despawn_time': spawn.despawn_time,
            'lat': spawn.lat,
            'lon': spawn.lon,
            'duration': spawn.duration
        } for spawn in spawns]

if conf.BOUNDARIES:
    from shapely.geometry import mapping

    def get_scan_coords():
        coordinates = mapping(conf.BOUNDARIES)['coordinates']
        coords = coordinates[0]
        markers = [{
                'type': 'scanarea',
                'coords': coords
            }]
        for blacklist in coordinates[1:]:
            markers.append({
                    'type': 'scanblacklist',
                    'coords': blacklist
                })
        return markers
else:
    def get_scan_coords():
        return ({
            'type': 'scanarea',
            'coords': (conf.MAP_START, (conf.MAP_START[0], conf.MAP_END[1]),
                       conf.MAP_END, (conf.MAP_END[0], conf.MAP_START[1]), conf.MAP_START)
        },)


def get_pokestop_markers():
    with session_scope() as session:
        pokestops = get_pokestops(session)
        return [{
            'id': pokestop.id,
            'external_id': pokestop.external_id,
            'lat': pokestop.lat,
            'lon': pokestop.lon,
            'name': pokestop.name,
            'url': pokestop.url,
            'updated': pokestop.updated,
            'quest_id': pokestop.quest_id,
            'pokestop_id': pokestop.pokestop_id,
            'quest_type': pokestop.quest_type,
            'quest_type_raw': pokestop.quest_type_raw,
            'item_type': pokestop.item_type,
            'item_amount': pokestop.item_amount,
            'item_id': pokestop.item_id,
            'pokemon_id': pokestop.pokemon_id,
            'quest_reward_type': pokestop.quest_reward_type,
            'quest_reward_type_raw': pokestop.quest_reward_type_raw,
            'quest_target': pokestop.quest_target,
            'quest_task': pokestop.quest_task,
            'quest_condition': pokestop.quest_condition,
            'timestamp': pokestop.timestamp
        } for pokestop in pokestops]

def get_darkstop_markers():
    with session_scope() as session:
        darkstops = get_darkstops(session)
        return [{
            'id': darkstop.id,
            'external_id': darkstop.external_id,
            'lat': darkstop.lat,
            'lon': darkstop.lon,
            'name': darkstop.name,
            'url': darkstop.url,
            'updated': darkstop.updated,
            'incident_start': darkstop.incident_start,
            'incident_expiration': darkstop.incident_expiration,
            'incident_grunt_type': darkstop.incident_grunt_type,
            'last_modified': darkstop.last_modified,
            'last_updated': darkstop.last_updated,
            'lure_expiration': darkstop.lure_expiration,
            'lure_start': darkstop.lure_start
        } for darkstop in darkstops]

def get_quest_filter_options():
    with session_scope() as session:
        quests = get_grouped_quest_task(session)
        return [{
            'quest_task': quest.quest_task,
            'quest_reward_type_raw': quest.quest_reward_type_raw,
            'pokemon_id': quest.pokemon_id,
            'item_id': quest.item_id,
            'count': quest.count
        } for quest in quests]

def sighting_to_report_marker(sighting):
    return {
        'icon': 'static/monocle-icons/icons/{}.png'.format(sighting.pokemon_id),
        'lat': sighting.lat,
        'lon': sighting.lon,
    }

def get_all_parks():
    parks = []
    try:
        parks = load_pickle('parks', raise_exception=True)
    except (FileNotFoundError, TypeError, KeyError):
        # all osm parks at 07/17/2016
        api = overpy.Overpass()
        request = '[timeout:620][date:"2016-07-17T00:00:00Z"];(way["leisure"="park"];way["landuse"="recreation_ground"];way["leisure"="recreation_ground"];way["leisure"="pitch"];way["leisure"="garden"];way["leisure"="golf_course"];way["leisure"="playground"];way["landuse"="meadow"];way["landuse"="grass"];way["landuse"="greenfield"];way["natural"="scrub"];way["natural"="heath"];way["natural"="grassland"];way["landuse"="farmyard"];way["landuse"="vineyard"];way["natural"="plateau"];way["leisure"="nature_reserve"];way["natural"="moor"];way["landuse"="farmland"];way["landuse"="orchard"];);out;>;out skel qt;'
        request = '[bbox:{},{},{},{}]{}'.format(south, west, north, east, request)
        response = api.query(request)
        for w in response.ways:
            parks.append({
                'type': 'park',
                'coords': [[float(c.lat), float(c.lon)] for c in w.nodes]
            })
        dump_pickle('parks', parks)

    return parks

def get_s2_cells(n=north, w=west, s=south, e=east, level=12):
    region_covered = s2sphere.LatLngRect.from_point_pair(
        s2sphere.LatLng.from_degrees(n, w),
        s2sphere.LatLng.from_degrees(s, e)
    )
    coverer = s2sphere.RegionCoverer()
    coverer.min_level = level
    coverer.max_level = level
    coverer.max_cells = 50
    covering = coverer.get_covering(region_covered)
    markers = []
    for cellid in covering:
        cell = s2sphere.Cell(cellid)
        markers.append({
            'id': 'cell-' + str(cellid.id()),
            'coords': [(get_vertex(cell, v)) for v in range(0, 4)]
        })
    return markers

def get_s2_cell_as_polygon(lat, lon, level=13):
    cell = s2sphere.Cell(s2sphere.CellId.from_lat_lng(s2sphere.LatLng.from_degrees(lat, lon)).parent(level))
    return [(get_vertex(cell, v)) for v in range(0, 4)]

def get_ex_gyms():
    ex_gyms = []
    parks = get_all_parks()
    try:
        ex_gyms = load_pickle('ex_gyms', raise_exception=True)
    except (FileNotFoundError, TypeError, KeyError):
        for g in get_gym_markers():
            g['id'] = 'ex-' + g['id']
            gym_point = Point(g['lat'], g['lon'])
            cell = Polygon(get_s2_cell_as_polygon(g['lat'], g['lon'], level=20)) # s2 lvl 20
            sponsor_value = 'park'
            if g['sponsor'] == 7:
                sponsor_value = 'starbucks'
                ex_gyms.append({
                            'id': g['id'],
                            'external_id': g['external_id'],
                            'fort_id': g['fort_id'],
                            'name': g['gym_name'],
                            'lat': g['lat'],
                            'lon': g['lon'],
                            'sponsor': sponsor_value
                })
            if g['sponsor'] == 9:
                sponsor_value = 'sprint'
                ex_gyms.append({
                            'id': g['id'],
                            'external_id': g['external_id'],
                            'fort_id': g['fort_id'],
                            'name': g['gym_name'],
                            'lat': g['lat'],
                            'lon': g['lon'],
                            'sponsor': sponsor_value
                })
            for p in parks:
                coords = p['coords']
                # osm polygon can be a line
                if len(coords) == 2:
                    shape = LineString(coords)
                    if shape.within(cell.centroid):
                        ex_gyms.append({
                            'id': g['id'],
                            'external_id': g['external_id'],
                            'fort_id': g['fort_id'],
                            'name': g['gym_name'],
                            'lat': g['lat'],
                            'lon': g['lon'],
                            'sponsor': sponsor_value
                        })
                if len(coords) > 2:
                    shape = Polygon(coords)
                    if shape.contains(cell.centroid):
                        ex_gyms.append({
                            'id': g['id'],
                            'external_id': g['external_id'],
                            'fort_id': g['fort_id'],
                            'name': g['gym_name'],
                            'lat': g['lat'],
                            'lon': g['lon'],
                            'sponsor': sponsor_value
                        })

        dump_pickle('ex_gyms', ex_gyms)
    return ex_gyms


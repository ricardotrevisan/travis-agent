import json
import unittest
from unittest.mock import MagicMock, patch

from runtime.models import RequestContext
from skills.route_planner import RoutePlannerSkill

_SP = (-23.5505, -46.6333)
_FLORIPA = (-27.5954, -48.5480)
_CURITIBA = (-25.4284, -49.2733)
_CAMPINAS = (-22.9099, -47.0626)
_RIBEIRAO = (-21.1704, -47.8103)
_UBERLANDIA = (-18.9187, -48.2772)

_ROUTE_SP_FLORIPA = {
    "total_km": 720.0,
    "total_minutes": 480,
    "coordinates": [
        _SP,
        (-24.5, -47.8),
        (-25.4, -49.2),
        (-26.3, -48.9),
        (-27.0, -48.7),
        _FLORIPA,
    ],
}

_CTX = RequestContext(
    sender="5511999999999@s.whatsapp.net",
    instance_name="Travis",
    message_id="m1",
    user_text="rota de São Paulo para Florianópolis",
)


def _make_skill():
    return RoutePlannerSkill()


def _make_cached_route():
    return {
        "origin": "São Paulo, SP",
        "destination": "Florianópolis, SC",
        "total_km": 720.0,
        "total_minutes": 480,
        "coordinates": list(_ROUTE_SP_FLORIPA["coordinates"]),
        "stops": [
            {"type": "rest", "name": "Curitiba", "lat": -25.4, "lon": -49.2,
             "km_from_origin": 400.0, "eta_minutes": 267, "detour_km": None, "pois": []},
        ],
    }


class RoutePlannerBasicTests(unittest.TestCase):

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_basic_route_returns_ok(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {"origin": "São Paulo, SP", "destination": "Florianópolis, SC"})
        self.assertTrue(result.ok)
        self.assertIn("São Paulo", result.user_visible_text)
        self.assertIn("Florianópolis", result.user_visible_text)
        self.assertIn("720", result.user_visible_text)

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_output_contains_route_metadata(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {"origin": "São Paulo, SP", "destination": "Florianópolis, SC"})
        self.assertEqual(result.output["total_km"], 720.0)
        self.assertEqual(result.output["estimated_hours"], 8.0)
        self.assertIsInstance(result.output["stops"], list)

    def test_missing_origin_returns_error(self):
        skill = _make_skill()
        result = skill.run(_CTX, {"destination": "Florianópolis, SC"})
        self.assertFalse(result.ok)
        self.assertIn("origem", result.user_visible_text.lower())

    def test_missing_destination_returns_error(self):
        skill = _make_skill()
        result = skill.run(_CTX, {"origin": "São Paulo, SP"})
        self.assertFalse(result.ok)
        self.assertIn("destino", result.user_visible_text.lower())

    @patch("utils.geo_client.geocode", side_effect=ValueError("não encontrado"))
    def test_geocode_origin_failure_returns_error(self, mock_geo):
        skill = _make_skill()
        result = skill.run(_CTX, {"origin": "XYZ Inexistente", "destination": "Florianópolis, SC"})
        self.assertFalse(result.ok)
        self.assertIn("origem", result.user_visible_text.lower())

    @patch("utils.geo_client.get_route", side_effect=ValueError("rota não encontrada"))
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_route_failure_returns_error(self, mock_geo, mock_route):
        skill = _make_skill()
        result = skill.run(_CTX, {"origin": "São Paulo, SP", "destination": "Florianópolis, SC"})
        self.assertFalse(result.ok)
        self.assertIn("rota", result.user_visible_text.lower())

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_plan_has_no_fixed_pois_output_key(self, mock_geo, mock_route, mock_pois):
        # fixed_pois foi removido do plan — output não deve ter fixed_pois_omitted
        skill = _make_skill()
        result = skill.run(_CTX, {"origin": "São Paulo, SP", "destination": "Florianópolis, SC"})
        self.assertTrue(result.ok)
        self.assertNotIn("fixed_pois_omitted", result.output)

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_plan_rest_stops_have_no_pois(self, mock_geo, mock_route, mock_pois):
        # Paradas de descanso no plan não carregam POIs — só nome da localidade
        skill = _make_skill()
        result = skill.run(_CTX, {"origin": "São Paulo, SP", "destination": "Florianópolis, SC"})
        self.assertTrue(result.ok)
        rest_stops = [s for s in result.output["stops"] if s["type"] == "rest"]
        for stop in rest_stops:
            self.assertEqual(stop["pois"], [])

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_plan_invites_poi_search(self, mock_geo, mock_route, mock_pois):
        # Texto de saída do plan deve mencionar pontos de interesse como próximo passo
        skill = _make_skill()
        result = skill.run(_CTX, {"origin": "São Paulo, SP", "destination": "Florianópolis, SC"})
        self.assertTrue(result.ok)
        self.assertIn("pontos de interesse", result.user_visible_text.lower())


class RoutePlannerStopsTests(unittest.TestCase):

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA, _CURITIBA])
    def test_fixed_waypoint_appears_in_stops(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "fixed_waypoints": ["Curitiba, PR"],
        })
        self.assertTrue(result.ok)
        types = [s["type"] for s in result.output["stops"]]
        self.assertIn("waypoint_fixed", types)

    @patch("utils.geo_client.driving_distance_m", return_value=500.0)
    @patch("utils.geo_client.get_pois", return_value=[{"name": "Posto BR", "type": "fuel", "lat": -25.0, "lon": -49.0, "has_brand": True, "place_id": "p1"}])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_fuel_stops_inserted_when_enabled(self, mock_geo, mock_route, mock_pois, mock_dist):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "fuel": {"enabled": True, "max_interval_km": 200, "tank_km_remaining": 250},
        })
        self.assertTrue(result.ok)
        self.assertGreater(result.output["fuel_stops_count"], 0)
        types = [s["type"] for s in result.output["stops"]]
        self.assertIn("fuel", types)

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_fuel_disabled_no_fuel_stops(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
        })
        self.assertTrue(result.ok)
        self.assertEqual(result.output["fuel_stops_count"], 0)

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_fuel_gap_when_no_station_found(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "fuel": {"enabled": True, "max_interval_km": 180, "tank_km_remaining": 200},
        })
        self.assertTrue(result.ok)
        self.assertEqual(result.output["fuel_stops_count"], 0)
        self.assertTrue(result.output["fuel_gaps_km"])
        self.assertIn("Sem posto mapeado", result.user_visible_text)

    @patch("utils.geo_client.driving_distance_m", return_value=500.0)
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_fuel_station_found_at_offset_along_route(self, mock_geo, mock_route, mock_dist):
        def _pois(lat, lon, radius, categories):
            if lat <= -24.5:
                return [{"name": "Posto Estrada", "type": "fuel", "lat": lat, "lon": lon, "has_brand": True, "place_id": f"p{lat}"}]
            return []
        skill = _make_skill()
        with patch("utils.geo_client.get_pois", side_effect=_pois) as mock_pois:
            result = skill.run(_CTX, {
                "origin": "São Paulo, SP",
                "destination": "Florianópolis, SC",
                "fuel": {"enabled": True, "max_interval_km": 180, "tank_km_remaining": 200},
            })
        self.assertTrue(result.ok)
        self.assertGreater(result.output["fuel_stops_count"], 0)
        self.assertEqual(result.output["fuel_gaps_km"], [])
        for call in mock_pois.call_args_list:
            self.assertEqual(call.args[2], _FUEL_SEARCH_RADIUS_M)

    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_consecutive_fuel_gap_never_exceeds_autonomy(self, mock_geo, mock_route):
        first = {"n": 0}
        def _pois(lat, lon, radius, categories):
            first["n"] += 1
            if first["n"] % 7 == 0:
                return [{"name": "Posto X", "type": "fuel", "lat": lat, "lon": lon}]
            return []
        with patch("utils.geo_client.get_pois", side_effect=_pois):
            result = _make_skill().run(_CTX, {
                "origin": "São Paulo, SP",
                "destination": "Florianópolis, SC",
                "fuel": {"enabled": True, "max_interval_km": 180, "tank_km_remaining": 200},
            })
        self.assertTrue(result.ok)
        fuel = [s for s in result.output["stops"] if s["type"] == "fuel"]
        kms = sorted(s["km_from_origin"] for s in fuel)
        prev = 0.0
        for km in kms:
            self.assertLessEqual(km - prev, 170.0 + 15.0 + 0.5)
            prev = km


class RoutePlannerIntervalTests(unittest.TestCase):

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_stop_interval_hours_overrides_km(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result_hours = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "stop_interval_hours": 2.0,
        })
        self.assertTrue(result_hours.ok)
        rest_stops_h = [s for s in result_hours.output["stops"] if s["type"] == "rest"]

        with patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA]):
            result_km = skill.run(_CTX, {
                "origin": "São Paulo, SP",
                "destination": "Florianópolis, SC",
                "stop_interval_km": 360,
            })
        rest_stops_km = [s for s in result_km.output["stops"] if s["type"] == "rest"]
        self.assertGreaterEqual(len(rest_stops_h), len(rest_stops_km))

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_max_stops_limits_rest_stops(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "stop_interval_km": 50,
            "max_stops": 2,
        })
        self.assertTrue(result.ok)
        rest = [s for s in result.output["stops"] if s["type"] == "rest"]
        self.assertLessEqual(len(rest), 2)


class RoutePlannerMultiWaypointTests(unittest.TestCase):

    _ROUTE_SP_UDI = {
        "total_km": 600.0,
        "total_minutes": 420,
        "coordinates": [_SP, _CAMPINAS, (-22.0, -47.5), _RIBEIRAO, (-20.0, -48.0), _UBERLANDIA],
    }

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_UDI)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _UBERLANDIA, _CAMPINAS, _RIBEIRAO])
    def test_multiple_waypoints_all_appear_in_stops(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Uberlândia, MG",
            "fixed_waypoints": ["Campinas, SP", "Ribeirão Preto, SP"],
        })
        self.assertTrue(result.ok)
        fixed = [s for s in result.output["stops"] if s["type"] == "waypoint_fixed"]
        self.assertEqual(len(fixed), 2)
        names = [s["name"] for s in fixed]
        self.assertIn("Campinas, SP", names)
        self.assertIn("Ribeirão Preto, SP", names)

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_UDI)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _UBERLANDIA, _CAMPINAS, _RIBEIRAO])
    def test_multiple_waypoints_ordered_by_km(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Uberlândia, MG",
            "fixed_waypoints": ["Campinas, SP", "Ribeirão Preto, SP"],
        })
        self.assertTrue(result.ok)
        kms = [s["km_from_origin"] for s in result.output["stops"]]
        self.assertEqual(kms, sorted(kms))

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_UDI)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _UBERLANDIA, _CAMPINAS])
    def test_waypoints_as_string_normalized_to_list(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Uberlândia, MG",
            "fixed_waypoints": "Campinas, SP",
        })
        self.assertTrue(result.ok)
        fixed = [s for s in result.output["stops"] if s["type"] == "waypoint_fixed"]
        self.assertEqual(len(fixed), 1)
        self.assertEqual(fixed[0]["name"], "Campinas, SP")

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_UDI)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _UBERLANDIA, ValueError("não encontrado")])
    def test_unresolvable_waypoint_fails_route(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Uberlândia, MG",
            "fixed_waypoints": ["Cidade Inexistente XYZ"],
        })
        self.assertFalse(result.ok)
        self.assertIn("Cidade Inexistente XYZ", result.user_visible_text)


class RoutePlannerModeTests(unittest.TestCase):

    @patch("utils.geo_client.driving_distance_m", return_value=500.0)
    @patch("utils.geo_client.get_pois", return_value=[{"name": "Posto BR", "type": "fuel", "lat": -25.0, "lon": -49.0, "has_brand": True, "place_id": "p1"}])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_default_mode_motorcycle_applies_fuel_default(self, mock_geo, mock_route, mock_pois, mock_dist):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
        })
        self.assertTrue(result.ok)
        self.assertGreater(result.output["fuel_stops_count"], 0)


class RoutePlannerPoiSearchTests(unittest.TestCase):

    def test_poi_search_without_cached_route_returns_error(self):
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value={}):
            result = skill.run(_CTX, {"action": "poi_search"})
        self.assertFalse(result.ok)
        self.assertIn("rota", result.user_visible_text.lower())

    def test_poi_search_returns_candidates_list(self):
        cached = _make_cached_route()
        poi_result = {
            "place_id": "abc123",
            "name": "Cachoeira do Avencal",
            "type": "natural_feature",
            "lat": -25.5,
            "lon": -49.0,
            "rating": 4.7,
            "user_ratings_total": 312,
            "has_brand": False,
        }
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("utils.geo_client.get_pois", return_value=[poi_result]), \
             patch("utils.geo_client.detour_km", return_value=4.0):
            result = skill.run(_CTX, {"action": "poi_search"})
        self.assertTrue(result.ok)
        self.assertIsInstance(result.output["candidates"], list)
        self.assertGreater(len(result.output["candidates"]), 0)
        candidate = result.output["candidates"][0]
        self.assertEqual(candidate["name"], "Cachoeira do Avencal")
        self.assertEqual(candidate["rating"], 4.7)

    def test_poi_search_filters_low_rating(self):
        cached = _make_cached_route()
        low_rated = {
            "place_id": "xyz",
            "name": "Lugar Mediano",
            "type": "natural_feature",
            "lat": -25.5,
            "lon": -49.0,
            "rating": 3.8,
            "user_ratings_total": 200,
            "has_brand": False,
        }
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("utils.geo_client.get_pois", return_value=[low_rated]), \
             patch("utils.geo_client.detour_km", return_value=3.0):
            result = skill.run(_CTX, {"action": "poi_search"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["candidates"], [])

    def test_poi_search_filters_low_reviews(self):
        cached = _make_cached_route()
        few_reviews = {
            "place_id": "xyz",
            "name": "Lugar Novo",
            "type": "natural_feature",
            "lat": -25.5,
            "lon": -49.0,
            "rating": 4.9,
            "user_ratings_total": 5,
            "has_brand": False,
        }
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("utils.geo_client.get_pois", return_value=[few_reviews]), \
             patch("utils.geo_client.detour_km", return_value=3.0):
            result = skill.run(_CTX, {"action": "poi_search"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["candidates"], [])

    def test_poi_search_filters_excessive_detour(self):
        cached = _make_cached_route()
        far_poi = {
            "place_id": "xyz",
            "name": "Cachoeira Distante",
            "type": "natural_feature",
            "lat": -25.5,
            "lon": -49.0,
            "rating": 4.8,
            "user_ratings_total": 300,
            "has_brand": False,
        }
        skill = _make_skill()
        # desvio de 20km > limite de 12km para natural_feature
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("utils.geo_client.get_pois", return_value=[far_poi]), \
             patch("utils.geo_client.detour_km", return_value=20.0):
            result = skill.run(_CTX, {"action": "poi_search"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["candidates"], [])

    def test_poi_search_deduplicates_by_place_id(self):
        cached = _make_cached_route()
        same_poi = {
            "place_id": "dup123",
            "name": "Mirante da Serra",
            "type": "natural_feature",
            "lat": -25.5,
            "lon": -49.0,
            "rating": 4.5,
            "user_ratings_total": 100,
            "has_brand": False,
        }
        skill = _make_skill()
        # mesmo place_id retornado em cada ponto amostrado
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("utils.geo_client.get_pois", return_value=[same_poi]), \
             patch("utils.geo_client.detour_km", return_value=3.0):
            result = skill.run(_CTX, {"action": "poi_search"})
        self.assertTrue(result.ok)
        place_ids = [c["place_id"] for c in result.output["candidates"]]
        self.assertEqual(len(place_ids), len(set(place_ids)))

    def test_poi_search_blocks_out_of_scope_types(self):
        cached = _make_cached_route()
        hotel = {
            "place_id": "hotel1",
            "name": "Hotel Conforto",
            "type": "lodging",
            "lat": -25.5,
            "lon": -49.0,
            "rating": 4.8,
            "user_ratings_total": 500,
            "has_brand": False,
        }
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("utils.geo_client.get_pois", return_value=[hotel]), \
             patch("utils.geo_client.detour_km", return_value=1.0):
            result = skill.run(_CTX, {"action": "poi_search"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["candidates"], [])

    def test_poi_search_output_sorted_by_km(self):
        cached = _make_cached_route()
        pois = [
            {"place_id": "b", "name": "B", "type": "natural_feature", "lat": -27.0, "lon": -48.7,
             "rating": 4.5, "user_ratings_total": 60, "has_brand": False},
            {"place_id": "a", "name": "A", "type": "natural_feature", "lat": -24.5, "lon": -47.8,
             "rating": 4.6, "user_ratings_total": 80, "has_brand": False},
        ]
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("utils.geo_client.get_pois", return_value=pois), \
             patch("utils.geo_client.detour_km", return_value=3.0):
            result = skill.run(_CTX, {"action": "poi_search"})
        self.assertTrue(result.ok)
        kms = [c["km_from_origin"] for c in result.output["candidates"]]
        self.assertEqual(kms, sorted(kms))


class RoutePlannerAddPoisTests(unittest.TestCase):

    def test_add_pois_without_cached_route_returns_error(self):
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value={}):
            result = skill.run(_CTX, {"action": "add_pois", "pois": []})
        self.assertFalse(result.ok)
        self.assertIn("rota", result.user_visible_text.lower())

    def test_add_pois_inserts_poi_fixed_stop(self):
        cached = _make_cached_route()
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("skills.route_planner._save_last_route"), \
             patch("utils.geo_client.detour_km", return_value=4.0):
            result = skill.run(_CTX, {"action": "add_pois", "pois": [
                {"place_id": "abc", "name": "Cachoeira do Avencal",
                 "lat": -25.5, "lon": -49.0, "type": "natural_feature",
                 "rating": 4.7, "user_ratings_total": 312},
            ]})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["pois_added"], 1)
        types = [s["type"] for s in result.output["stops"]]
        self.assertIn("poi_fixed", types)

    def test_add_pois_omits_when_detour_exceeds_limit(self):
        cached = _make_cached_route()
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("skills.route_planner._save_last_route"), \
             patch("utils.geo_client.detour_km", return_value=25.0):
            result = skill.run(_CTX, {"action": "add_pois", "pois": [
                {"place_id": "abc", "name": "Ponto Distante",
                 "lat": -25.5, "lon": -49.0, "type": "natural_feature"},
            ]})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["pois_added"], 0)
        self.assertIn("Ponto Distante", result.output["pois_omitted"])
        self.assertIn("⚠️", result.user_visible_text)

    def test_add_pois_preserves_existing_stops(self):
        cached = _make_cached_route()
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("skills.route_planner._save_last_route"), \
             patch("utils.geo_client.detour_km", return_value=3.0):
            result = skill.run(_CTX, {"action": "add_pois", "pois": [
                {"place_id": "new1", "name": "Novo Ponto",
                 "lat": -26.0, "lon": -49.0, "type": "natural_feature"},
            ]})
        self.assertTrue(result.ok)
        types = [s["type"] for s in result.output["stops"]]
        self.assertIn("rest", types)
        self.assertIn("poi_fixed", types)

    def test_add_pois_result_ordered_by_km(self):
        cached = _make_cached_route()
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("skills.route_planner._save_last_route"), \
             patch("utils.geo_client.detour_km", return_value=3.0):
            result = skill.run(_CTX, {"action": "add_pois", "pois": [
                {"place_id": "early", "name": "Ponto Inicial",
                 "lat": _SP[0], "lon": _SP[1], "type": "natural_feature"},
                {"place_id": "late", "name": "Ponto Final",
                 "lat": _FLORIPA[0], "lon": _FLORIPA[1], "type": "natural_feature"},
            ]})
        self.assertTrue(result.ok)
        kms = [s["km_from_origin"] for s in result.output["stops"]]
        self.assertEqual(kms, sorted(kms))

    def _cached_with_candidates(self):
        cached = _make_cached_route()
        cached["poi_candidates"] = [
            {"place_id": "p1", "name": "Von Strudel", "type": "restaurant",
             "lat": -23.5, "lon": -47.4, "km_from_origin": 72.0, "eta_minutes": 58,
             "detour_km": 0.4, "rating": 4.7, "user_ratings_total": 1918},
            {"place_id": "p2", "name": "Haras GKF", "type": "restaurant",
             "lat": -23.6, "lon": -47.5, "km_from_origin": 102.0, "eta_minutes": 82,
             "detour_km": 2.4, "rating": 4.5, "user_ratings_total": 58},
        ]
        return cached

    def test_add_pois_resolves_by_indices_field(self):
        # campo canônico: indices=[1, 2]
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=self._cached_with_candidates()), \
             patch("skills.route_planner._save_last_route"), \
             patch("utils.geo_client.detour_km", return_value=2.0):
            result = skill.run(_CTX, {"action": "add_pois", "indices": [1, 2]})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["pois_added"], 2)
        names = [s["name"] for s in result.output["stops"] if s["type"] == "poi_fixed"]
        self.assertIn("Von Strudel", names)
        self.assertIn("Haras GKF", names)

    def test_add_pois_resolves_by_poi_indices_field(self):
        # variação que o planner às vezes manda: poi_indices=[18, 24]
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=self._cached_with_candidates()), \
             patch("skills.route_planner._save_last_route"), \
             patch("utils.geo_client.detour_km", return_value=2.0):
            result = skill.run(_CTX, {"action": "add_pois", "poi_indices": [1, 2]})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["pois_added"], 2)

    def test_add_pois_resolves_by_pois_to_add_with_id(self):
        # variação com lista de dicts: pois_to_add=[{id: 1, name: ...}]
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=self._cached_with_candidates()), \
             patch("skills.route_planner._save_last_route"), \
             patch("utils.geo_client.detour_km", return_value=2.0):
            result = skill.run(_CTX, {"action": "add_pois", "pois_to_add": [
                {"id": 1, "name": "Von Strudel"},
                {"id": 2, "name": "Haras GKF"},
            ]})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["pois_added"], 2)

    def test_add_pois_index_without_candidates_in_cache_is_silently_skipped(self):
        # Se o cache não tiver poi_candidates e o planner mandar só índice, não deve crashar.
        cached = _make_cached_route()  # sem poi_candidates
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("skills.route_planner._save_last_route"):
            result = skill.run(_CTX, {"action": "add_pois", "indices": [1]})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["pois_added"], 0)

    def test_add_pois_renews_cache(self):
        cached = _make_cached_route()
        skill = _make_skill()
        with patch("skills.route_planner._load_last_route", return_value=cached), \
             patch("skills.route_planner._save_last_route") as mock_save, \
             patch("utils.geo_client.detour_km", return_value=3.0):
            skill.run(_CTX, {"action": "add_pois", "pois": [
                {"place_id": "x", "name": "Ponto X",
                 "lat": -25.5, "lon": -49.0, "type": "natural_feature"},
            ]})
        mock_save.assert_called_once()


# importa a constante para o teste de raio de abastecimento
from skills.route_planner import _FUEL_SEARCH_RADIUS_M  # noqa: E402

if __name__ == "__main__":
    unittest.main()

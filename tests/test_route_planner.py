import unittest
from unittest.mock import patch

from runtime.models import RequestContext
from skills.route_planner import RoutePlannerSkill

_SP = (-23.5505, -46.6333)
_FLORIPA = (-27.5954, -48.5480)
_CURITIBA = (-25.4284, -49.2733)
_VILA_VELHA = (-25.2254, -50.0021)
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

    @patch("utils.geo_client.detour_km", return_value=5.0)
    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA, _VILA_VELHA])
    def test_fixed_poi_within_detour_included(self, mock_geo, mock_route, mock_pois, mock_detour):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "fixed_pois": [{"name": "Parque Vila Velha", "location": "Ponta Grossa, PR", "max_detour_km": 15}],
        })
        self.assertTrue(result.ok)
        types = [s["type"] for s in result.output["stops"]]
        self.assertIn("poi_fixed", types)
        self.assertEqual(result.output["fixed_pois_omitted"], [])

    @patch("utils.geo_client.detour_km", return_value=30.0)
    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA, _VILA_VELHA])
    def test_fixed_poi_exceeds_detour_is_omitted(self, mock_geo, mock_route, mock_pois, mock_detour):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "fixed_pois": [{"name": "Parque Vila Velha", "location": "Ponta Grossa, PR", "max_detour_km": 15}],
        })
        self.assertTrue(result.ok)
        types = [s["type"] for s in result.output["stops"]]
        self.assertNotIn("poi_fixed", types)
        self.assertIn("Parque Vila Velha", result.output["fixed_pois_omitted"])
        self.assertIn("⚠️", result.user_visible_text)

    @patch("utils.geo_client.get_pois", return_value=[{"name": "Posto BR", "type": "fuel", "lat": -25.0, "lon": -49.0}])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_fuel_stops_inserted_when_enabled(self, mock_geo, mock_route, mock_pois):
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
        # Nenhum posto em nenhum raio → trechos viram fuel_gaps com aviso de autonomia.
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

    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_fuel_station_found_only_in_wider_radius(self, mock_geo, mock_route):
        # Raio 5km vazio, 10km acha: parada deve ser criada, sem gap.
        def _pois(lat, lon, radius, categories):
            if "posto" in (categories[0] if categories else "") and radius >= 10000:
                return [{"name": "Posto Estrada", "type": "fuel", "lat": lat, "lon": lon}]
            return []
        skill = _make_skill()
        with patch("utils.geo_client.get_pois", side_effect=_pois):
            result = skill.run(_CTX, {
                "origin": "São Paulo, SP",
                "destination": "Florianópolis, SC",
                "fuel": {"enabled": True, "max_interval_km": 180, "tank_km_remaining": 200},
            })
        self.assertTrue(result.ok)
        self.assertGreater(result.output["fuel_stops_count"], 0)
        self.assertEqual(result.output["fuel_gaps_km"], [])


class RoutePlannerIntervalTests(unittest.TestCase):

    @patch("utils.geo_client.get_pois", return_value=[])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_stop_interval_hours_overrides_km(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        # 2h interval on 480min/720km route → ~180km interval → ~3 rest stops
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
        # hours-based with 2h @ 90km/h = 180km interval should yield more stops than 360km interval
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
        "coordinates": [
            _SP,
            _CAMPINAS,
            (-22.0, -47.5),
            _RIBEIRAO,
            (-20.0, -48.0),
            _UBERLANDIA,
        ],
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
    @patch("utils.geo_client.geocode", side_effect=[_SP, _UBERLANDIA, _CAMPINAS, _RIBEIRAO])
    def test_waypoints_coexist_with_fuel_and_rest(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Uberlândia, MG",
            "fixed_waypoints": ["Campinas, SP", "Ribeirão Preto, SP"],
            "fuel": {"enabled": False},
        })
        self.assertTrue(result.ok)
        types = {s["type"] for s in result.output["stops"]}
        self.assertIn("waypoint_fixed", types)

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

    @patch("utils.geo_client.get_pois", return_value=[{"name": "Posto BR", "type": "fuel", "lat": -25.0, "lon": -49.0}])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_default_mode_motorcycle_applies_fuel_default(self, mock_geo, mock_route, mock_pois):
        # Sem fuel nos args, o default de moto (180km/200km) deve gerar abastecimento.
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
        })
        self.assertTrue(result.ok)
        self.assertGreater(result.output["fuel_stops_count"], 0)


class RoutePlannerPOITests(unittest.TestCase):

    @patch("utils.geo_client.get_pois", return_value=[
        {"name": "Restaurante Boa Mesa", "type": "restaurant", "lat": -25.0, "lon": -49.0},
    ])
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_poi_enriches_rest_stop(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "preferences": ["restaurante"],
        })
        self.assertTrue(result.ok)
        rest_stops = [s for s in result.output["stops"] if s["type"] == "rest"]
        if rest_stops:
            self.assertTrue(any(s["pois"] for s in rest_stops))

    @patch("utils.geo_client.get_pois", side_effect=Exception("places down"))
    @patch("utils.geo_client.get_route", return_value=_ROUTE_SP_FLORIPA)
    @patch("utils.geo_client.geocode", side_effect=[_SP, _FLORIPA])
    def test_places_failure_does_not_crash(self, mock_geo, mock_route, mock_pois):
        skill = _make_skill()
        # desativa fuel para isolar o teste de POIs de descanso
        result = skill.run(_CTX, {
            "origin": "São Paulo, SP",
            "destination": "Florianópolis, SC",
            "preferences": ["restaurante"],
            "fuel": {"enabled": False},
        })
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()

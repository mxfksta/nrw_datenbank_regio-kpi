import pytest
import responses

from src.net import RobotsDisallowedError, http_get


@responses.activate
def test_robots_disallow_wird_respektiert(settings):
    responses.add(
        responses.GET,
        "https://example.org/robots.txt",
        body="User-agent: *\nDisallow: /",
    )
    with pytest.raises(RobotsDisallowedError):
        http_get("https://example.org/daten.csv", settings)


@responses.activate
def test_user_agent_wird_gesetzt(settings):
    responses.add(
        responses.GET,
        "https://example.org/robots.txt",
        body="User-agent: *\nAllow: /",
    )
    responses.add(responses.GET, "https://example.org/daten.csv", body="a;b\n1;2\n")
    resp = http_get("https://example.org/daten.csv", settings)
    assert resp.status_code == 200
    assert "regio-kpi-pipeline" in resp.request.headers["User-Agent"]


@responses.activate
def test_4xx_wird_sofort_geworfen(settings):
    responses.add(
        responses.GET,
        "https://example.org/robots.txt",
        body="User-agent: *\nAllow: /",
    )
    responses.add(responses.GET, "https://example.org/fehlt.csv", status=404)
    import requests

    with pytest.raises(requests.HTTPError):
        http_get("https://example.org/fehlt.csv", settings)

import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .services import station_index
from .services.geocoding import GeocodingError, suggest_places
from .services.optimizer import InfeasibleRouteError
from .services.osrm import NoRouteError, RoutingError
from .services.planner import PlanRequest, build_plan

log = logging.getLogger(__name__)


class BadRequest(ValueError):
    pass


def _params(request) -> dict:
    if request.method == "POST":
        if request.content_type == "application/json":
            try:
                body = json.loads(request.body or b"{}")
            except json.JSONDecodeError as exc:
                raise BadRequest(f"Invalid JSON body: {exc}") from exc
            if not isinstance(body, dict):
                raise BadRequest("JSON body must be an object.")
            return body
        return request.POST.dict()
    return request.GET.dict()


def _float(params, name, default, lo, hi):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"'{name}' must be a number.") from None
    if not lo <= val <= hi:
        raise BadRequest(f"'{name}' must be between {lo} and {hi}.")
    return val


def _plan_request(request) -> PlanRequest:
    params = _params(request)
    start = str(params.get("start") or "").strip()
    finish = str(params.get("finish") or "").strip()
    if not start or not finish:
        raise BadRequest("Both 'start' and 'finish' are required, e.g. ?start=Chicago, IL&finish=Denver, CO")
    raw_route = params.get("route")
    if raw_route in (None, ""):
        route_index = 0
    else:
        try:
            route_index = int(raw_route)
        except (TypeError, ValueError):
            raise BadRequest("'route' must be 0 or 1.") from None
        if route_index not in (0, 1):
            raise BadRequest("'route' must be 0 or 1.")
    return PlanRequest(
        start=start,
        finish=finish,
        start_fuel=_float(params, "start_fuel", 1.0, 0.0, 1.0),
        corridor_miles=_float(params, "corridor_miles", None, 0.5, 50.0),
        stop_penalty=_float(params, "stop_penalty", None, 0.0, 500.0),
        route_index=route_index,
    )


def _error(message, status):
    return JsonResponse({"error": message}, status=status)


def _run(plan_req: PlanRequest):
    """Returns (payload, None) or (None, JsonResponse error)."""
    try:
        return build_plan(plan_req), None
    except GeocodingError as exc:
        return None, _error(str(exc), 400)
    except NoRouteError as exc:
        return None, _error(str(exc), 422)
    except InfeasibleRouteError as exc:
        return None, _error(str(exc), 422)
    except RoutingError as exc:
        log.warning("routing upstream failure: %s", exc)
        return None, _error(str(exc), 502)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def route_plan(request):
    """Route + optimal fuel stops + total cost, as JSON."""
    try:
        plan_req = _plan_request(request)
    except BadRequest as exc:
        return _error(str(exc), 400)

    payload, err = _run(plan_req)
    if err:
        return err

    query = {
        "start": plan_req.start,
        "finish": plan_req.finish,
        "start_fuel": plan_req.start_fuel,
        "route": payload["selected_route"],
    }
    for name in ("corridor_miles", "stop_penalty"):
        if getattr(plan_req, name) is not None:
            query[name] = getattr(plan_req, name)
    payload["map_url"] = request.build_absolute_uri(reverse("route-map") + "?" + urlencode(query))
    return JsonResponse(payload)


@require_GET
def route_map(request):
    """Interactive Leaflet map of the route and fuel stops (HTML)."""
    context = {"query": request.GET.dict()}
    if request.GET.get("start") or request.GET.get("finish"):
        try:
            plan_req = _plan_request(request)
        except BadRequest as exc:
            context["error"] = str(exc)
        else:
            payload, err = _run(plan_req)
            if err:
                context["error"] = json.loads(err.content)["error"]
            else:
                base = request.GET.copy()
                for opt in payload["options"]:
                    q = base.copy()
                    q["route"] = str(opt["index"])
                    opt["choose_url"] = "?" + q.urlencode()
                context["plan"] = payload
    return render(request, "routing/map.html", context, status=200 if "error" not in context else 400)


@require_GET
def place_suggestions(request):
    """Autocomplete for start/finish: GET /api/places/?q=new&limit=8 (local data, no external calls)."""
    q = request.GET.get("q", "")
    if len(q) > 100:
        return _error("'q' is too long.", 400)
    try:
        limit = min(max(int(request.GET.get("limit", 8)), 1), 20)
    except ValueError:
        return _error("'limit' must be an integer.", 400)
    response = JsonResponse({"query": q, "results": suggest_places(q, limit)})
    response["Cache-Control"] = "public, max-age=86400"
    return response


@require_GET
def health(request):
    return JsonResponse({"status": "ok", "stations_loaded": len(station_index.get_index())})

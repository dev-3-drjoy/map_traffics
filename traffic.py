"""
traffic.py — Kiểm tra giao thông dùng Google Routes API (v2).

Kiến trúc:
  - 2 lần computeRoutes (overview polyline + route có intermediate waypoints)
  - Ghim / geocode: điểm trên polyline leg (theo quãng đường), không dùng trung điểm hình học
  - Mỗi đoạn tắc: 1 Geocoding reverse tại điểm ghim để lấy tên đường (route)

Quota Routes: xem Google Cloud; Geocoding tính theo request riêng.
"""

import os
import re
import math
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GOOGLE_MAPS_KEY = os.getenv("GOOGLE_MAPS_KEY")

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Khoảng cách mỗi sub-segment (mét) — nhỏ hơn → chi tiết hơn nhưng nhiều waypoints hơn
SUBSEGMENT_SIZE_METERS = 300

# Routes API giới hạn tối đa 25 intermediate waypoints
MAX_INTERMEDIATE_WAYPOINTS = 25

# Bỏ qua leg quá ngắn khi báo cáo (rẽ ngã tư, vòng xuyến...)
MIN_REPORT_METERS = 100

# Điểm ghim Maps / geocode: phần quãng đường dọc polyline (0..1)
PIN_PATH_FRACTION = 0.5


def check_traffic(origin: str, destination: str) -> dict:
    """
    Kiểm tra giao thông từ origin đến destination.

    Bước 1 — Lấy polyline tổng quan (computeRoutes, không cần field mask traffic, rất rẻ)
    Bước 2 — Sample điểm dọc polyline làm intermediate waypoints
    Bước 3 — Gọi lại computeRoutes với waypoints → nhận traffic từng leg → 1 request duy nhất

    Trả về dict: status, duration_normal, duration_traffic, distance,
                 congested_segments, timestamp
    """
    if not GOOGLE_MAPS_KEY:
        raise ValueError("Thiếu GOOGLE_MAPS_KEY trong biến môi trường!")

    # ── Bước 1: Lấy polyline tổng quan ────────────────────────────────────────
    overview = _get_overview_route(origin, destination)
    total_normal   = overview["duration_normal"]
    total_traffic  = overview["duration_traffic"]
    total_distance = overview["distance"]
    polyline_points = overview["polyline_points"]

    # ── Bước 2: Sample waypoints từ polyline ──────────────────────────────────
    waypoints = _sample_waypoints(polyline_points)
    logger.info(f"Routes API: {len(waypoints)} intermediate waypoint(s) từ polyline")

    # ── Bước 3: Gọi Routes API với waypoints, lấy traffic từng leg ────────────
    legs = _get_legs_with_traffic(origin, destination, waypoints)
    congested_segments = _find_congested_segments(legs)
    congested_segments = _enrich_congested_segments_geocode(congested_segments)

    return {
        "status":              classify_traffic(total_normal, total_traffic),
        "duration_normal":     total_normal,
        "duration_traffic":    total_traffic,
        "distance":            total_distance,
        "congested_segments":  congested_segments,
        "timestamp":           datetime.now().strftime("%H:%M %d/%m/%Y"),
    }


# ── Bước 1: Overview route ─────────────────────────────────────────────────────

def _get_overview_route(origin: str, destination: str) -> dict:
    """
    Gọi Routes API lần đầu chỉ để lấy:
      - tổng duration / duration_in_traffic / distance
      - encoded polyline tổng quan để sample waypoints

    Field mask tối giản → tiết kiệm quota tính theo field.
    """
    body = {
        "origin":      _make_location(origin),
        "destination": _make_location(destination),
        "travelMode":  "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "languageCode": "vi",
        "regionCode":   "VN",
    }
    headers = _routes_headers(
        "routes.duration,routes.staticDuration,routes.distanceMeters,routes.polyline"
    )

    resp = requests.post(ROUTES_URL, json=body, headers=headers, timeout=15)
    _raise_for_routes_error(resp)
    data = resp.json()

    if not data.get("routes"):
        raise Exception("Không tìm thấy tuyến đường. Vui lòng kiểm tra lại địa điểm.")

    route = data["routes"][0]

    # duration có traffic (string dạng "1234s"), staticDuration không có traffic
    duration_traffic = _parse_duration(route.get("duration", "0s"))
    duration_normal  = _parse_duration(route.get("staticDuration", "0s"))
    distance         = route.get("distanceMeters", 0)

    encoded = route.get("polyline", {}).get("encodedPolyline", "")
    polyline_points = _decode_polyline(encoded) if encoded else []

    logger.info(
        f"Overview: {distance/1000:.1f}km, "
        f"bình thường={duration_normal//60}p, thực tế={duration_traffic//60}p, "
        f"polyline={len(polyline_points)} điểm"
    )

    return {
        "duration_normal":  duration_normal,
        "duration_traffic": duration_traffic,
        "distance":         distance,
        "polyline_points":  polyline_points,
    }


# ── Bước 3: Legs với traffic ───────────────────────────────────────────────────

def _get_legs_with_traffic(origin: str, destination: str, waypoints: list) -> list:
    """
    Gọi Routes API lần 2 với intermediate waypoints.
    Trả về list legs, mỗi leg có: duration_normal, duration_traffic, distance_m, label.

    Routes API tính 1 request bất kể số waypoints → quota tối ưu.
    """
    body = {
        "origin":      _make_location(origin),
        "destination": _make_location(destination),
        "travelMode":  "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "languageCode": "vi",
        "regionCode":   "VN",
    }

    if waypoints:
        body["intermediates"] = [
            {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
            for lat, lng in waypoints
        ]

    headers = _routes_headers(
        "routes.legs.duration,routes.legs.staticDuration,"
        "routes.legs.distanceMeters,routes.legs.startLocation,routes.legs.endLocation,"
        "routes.legs.polyline,"
        "routes.legs.steps.navigationInstruction.instructions,"
        "routes.legs.steps.navigationInstruction.maneuver"
    )

    resp = requests.post(ROUTES_URL, json=body, headers=headers, timeout=20)
    _raise_for_routes_error(resp)
    data = resp.json()

    if not data.get("routes"):
        raise Exception("Không nhận được dữ liệu legs từ Routes API.")

    legs_raw = data["routes"][0].get("legs", [])
    legs = []
    for i, leg in enumerate(legs_raw):
        duration_traffic = _parse_duration(leg.get("duration", "0s"))
        duration_normal  = _parse_duration(leg.get("staticDuration", "0s"))
        distance_m       = leg.get("distanceMeters", 0)

        start = leg.get("startLocation", {}).get("latLng", {})
        end   = leg.get("endLocation",   {}).get("latLng", {})
        instruction_summary = _extract_leg_instruction(leg)

        enc_leg = leg.get("polyline", {}).get("encodedPolyline", "")
        leg_poly = _decode_polyline(enc_leg) if enc_leg else []

        legs.append({
            "index":            i,
            "duration_normal":  duration_normal,
            "duration_traffic": duration_traffic,
            "distance_m":       distance_m,
            "start":            (start.get("latitude", 0), start.get("longitude", 0)),
            "end":              (end.get("latitude", 0),   end.get("longitude", 0)),
            "instruction_summary": instruction_summary,
            "polyline_points":  leg_poly,
        })

    logger.info(f"Legs: {len(legs)} đoạn nhận được từ Routes API")
    return legs


# ── Phát hiện tắc đường ────────────────────────────────────────────────────────

def _find_congested_segments(legs: list) -> list:
    """
    Duyệt từng leg, gom các leg liền kề có cùng trạng thái tắc
    thành 1 đoạn để tránh báo lặp.
    """
    congested = []
    current_group = None

    for leg in legs:
        if leg["distance_m"] < MIN_REPORT_METERS:
            continue

        status = classify_traffic(leg["duration_normal"], leg["duration_traffic"])

        if status in ("yellow", "red"):
            delay_sec = leg["duration_traffic"] - leg["duration_normal"]

            if current_group and current_group["status"] == status:
                # Gộp vào group hiện tại
                current_group["distance_m"] += leg["distance_m"]
                current_group["delay_sec"]  += delay_sec
                current_group["end"]         = leg["end"]
                current_group["polyline_points"] = _concat_polyline_points(
                    current_group.get("polyline_points", []),
                    leg.get("polyline_points", []),
                )
                current_group["instruction"] = _merge_instruction_text(
                    current_group.get("instruction", ""),
                    leg.get("instruction_summary", ""),
                )
            else:
                if current_group:
                    congested.append(current_group)
                current_group = {
                    "status":     status,
                    "distance_m": leg["distance_m"],
                    "delay_sec":  delay_sec,
                    "start":      leg["start"],
                    "end":        leg["end"],
                    "instruction": leg.get("instruction_summary", ""),
                    "polyline_points": list(leg.get("polyline_points", [])),
                }
        else:
            if current_group:
                congested.append(current_group)
            current_group = None

    if current_group:
        congested.append(current_group)

    return congested


def _enrich_congested_segments_geocode(segments: list) -> list:
    """Thêm midpoint (trên polyline nếu có) và road_name (Geocoding reverse)."""
    enriched = []
    for seg in segments:
        start = seg["start"]
        end = seg["end"]
        pts = seg.get("polyline_points") or []
        pin = _point_at_path_fraction(pts, PIN_PATH_FRACTION)
        if pin is None:
            pin = _midpoint_latlng(start, end)
        road_name = _reverse_geocode_road_name(pin[0], pin[1])
        out = {k: v for k, v in seg.items() if k != "polyline_points"}
        enriched.append({
            **out,
            "midpoint": pin,
            "road_name": road_name,
        })
    return enriched


def _concat_polyline_points(a: list, b: list) -> list:
    """Nối hai polyline; bỏ điểm trùng ở nối."""
    if not a:
        return list(b)
    if not b:
        return list(a)
    out = list(a)
    start_b = 1 if _polyline_points_close(out[-1], b[0]) else 0
    out.extend(b[start_b:])
    return out


def _polyline_points_close(p1: tuple[float, float], p2: tuple[float, float]) -> bool:
    return _haversine_meters(p1, p2) < 3.0


def _polyline_path_length_m(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    return sum(
        _haversine_meters(points[i], points[i + 1])
        for i in range(len(points) - 1)
    )


def _point_at_path_fraction(
    points: list[tuple[float, float]],
    fraction: float,
) -> tuple[float, float] | None:
    """
    Điểm trên polyline theo phần quãng đường (Haversine) từ đầu đến cuối.
    """
    if len(points) < 2:
        return None
    total = _polyline_path_length_m(points)
    if total <= 0:
        return points[0]

    target = max(0.0, min(1.0, fraction)) * total
    cumulative = 0.0
    for i in range(len(points) - 1):
        p0, p1 = points[i], points[i + 1]
        seg_len = _haversine_meters(p0, p1)
        if seg_len < 1e-6:
            cumulative += seg_len
            continue
        if cumulative + seg_len >= target:
            t = (target - cumulative) / seg_len
            t = max(0.0, min(1.0, t))
            lat = p0[0] + t * (p1[0] - p0[0])
            lng = p0[1] + t * (p1[1] - p0[1])
            return (lat, lng)
        cumulative += seg_len
    return points[-1]


def _midpoint_latlng(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
    return ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)


def _reverse_geocode_road_name(lat: float, lng: float) -> str:
    """
    Reverse geocoding tại một điểm; ưu tiên address_components loại route (tên đường).
    """
    params = {
        "latlng": f"{lat},{lng}",
        "language": "vi",
        "region": "vn",
        "key": GOOGLE_MAPS_KEY,
    }
    try:
        resp = requests.get(GEOCODING_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("Geocoding reverse lỗi: %s", e)
        return ""

    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        logger.warning("Geocoding status: %s", data.get("status"))
        return ""

    for result in data.get("results", []):
        for comp in result.get("address_components", []):
            types = comp.get("types", [])
            if "route" in types:
                name = (comp.get("long_name") or "").strip()
                if name:
                    return name

    return ""


# ── Routes API helpers ─────────────────────────────────────────────────────────

def _make_location(address_or_latlng: str) -> dict:
    """
    Chuyển chuỗi địa chỉ hoặc 'lat,lng' thành format location của Routes API.
    """
    parts = address_or_latlng.strip().split(",")
    if len(parts) == 2:
        try:
            lat = float(parts[0].strip())
            lng = float(parts[1].strip())
            return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}
        except ValueError:
            pass
    # Là địa chỉ text
    return {"address": address_or_latlng}


def _routes_headers(field_mask: str) -> dict:
    return {
        "Content-Type":             "application/json",
        "X-Goog-Api-Key":           GOOGLE_MAPS_KEY,
        "X-Goog-FieldMask":         field_mask,
    }


def _extract_leg_instruction(leg: dict, max_steps: int = 2, max_len: int = 120) -> str:
    """
    Fallback mô tả leg: ưu tiên bước NAME_CHANGE (đổi tên đường), sau đó các bước khác.
    """
    name_change_texts = []
    other_texts = []
    for step in leg.get("steps", []):
        nav = step.get("navigationInstruction", {})
        text = _normalize_instruction(nav.get("instructions", ""))
        if not text:
            continue
        if nav.get("maneuver") == "NAME_CHANGE":
            name_change_texts.append(text)
        else:
            other_texts.append(text)

    step_texts = (name_change_texts + other_texts)[:max_steps]

    if not step_texts:
        return ""

    merged = " · ".join(step_texts)
    if len(merged) > max_len:
        return merged[: max_len - 3].rstrip() + "..."
    return merged


def _normalize_instruction(text: str) -> str:
    text = text.replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _merge_instruction_text(current: str, incoming: str, max_len: int = 180) -> str:
    if not current:
        return incoming
    if not incoming:
        return current
    if incoming in current:
        return current

    merged = f"{current} · {incoming}"
    if len(merged) > max_len:
        return merged[: max_len - 3].rstrip() + "..."
    return merged


def _parse_duration(duration_str: str) -> int:
    """Parse chuỗi dạng '1234s' thành số giây (int)."""
    return int(duration_str.rstrip("s")) if duration_str else 0


def _raise_for_routes_error(resp: requests.Response):
    if resp.status_code == 200:
        return
    try:
        err = resp.json()
        msg = err.get("error", {}).get("message", resp.text)
    except Exception:
        msg = resp.text
    error_map = {
        400: f"Yêu cầu không hợp lệ: {msg}",
        403: "API key không hợp lệ hoặc chưa bật Routes API.",
        429: "Đã vượt quota Routes API. Thử lại sau.",
    }
    raise Exception(error_map.get(resp.status_code, f"Routes API lỗi {resp.status_code}: {msg}"))


# ── Polyline helpers ───────────────────────────────────────────────────────────

def _decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode Google Encoded Polyline → list (lat, lng)."""
    points = []
    index = lat = lng = 0

    while index < len(encoded):
        result = shift = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lat += ~(result >> 1) if result & 1 else result >> 1

        result = shift = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lng += ~(result >> 1) if result & 1 else result >> 1

        points.append((lat / 1e5, lng / 1e5))

    return points


def _haversine_meters(p1: tuple, p2: tuple) -> float:
    R = 6_371_000
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _sample_waypoints(points: list, interval: float = SUBSEGMENT_SIZE_METERS) -> list[tuple]:
    """
    Lấy các điểm cách nhau ~interval mét dọc polyline.
    Bỏ điểm đầu và điểm cuối (đã là origin/destination).
    Giới hạn MAX_INTERMEDIATE_WAYPOINTS điểm.
    """
    if len(points) < 3:
        return []

    sampled = []
    accumulated = 0.0

    for i in range(1, len(points) - 1):
        accumulated += _haversine_meters(points[i - 1], points[i])
        if accumulated >= interval:
            sampled.append(points[i])
            accumulated = 0.0
            if len(sampled) >= MAX_INTERMEDIATE_WAYPOINTS:
                break

    return sampled


# ── Classify ───────────────────────────────────────────────────────────────────

def classify_traffic(duration_normal: int, duration_traffic: int) -> str:
    if duration_normal == 0:
        return "green"
    ratio = duration_traffic / duration_normal
    if ratio < 1.0:
        return "green"
    elif ratio < 1.4:
        return "yellow"
    else:
        return "red"
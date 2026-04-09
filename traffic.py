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
from urllib.parse import quote
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # bật DEBUG để xem Geocoding raw data; xoá dòng này sau khi debug xong

# Ghi log ra file để dễ đọc / search
_log_file_handler = logging.FileHandler("geocoding_debug.log", encoding="utf-8")
_log_file_handler.setLevel(logging.DEBUG)
_log_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logger.addHandler(_log_file_handler)

GOOGLE_MAPS_KEY = os.getenv("GOOGLE_MAPS_KEY")

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Khoảng cách mỗi sub-segment (mét) — nhỏ hơn → chi tiết hơn nhưng nhiều waypoints hơn
SUBSEGMENT_SIZE_METERS = 300

# Routes API giới hạn tối đa 25 intermediate waypoints
MAX_INTERMEDIATE_WAYPOINTS = 25

# Static Maps giới hạn ~8192 ký tự URL; nhiều path phủ đoạn tắc có thể cần fallback
STATIC_MAP_MAX_URL_LEN = 7800

# Bỏ qua leg quá ngắn khi báo cáo (rẽ ngã tư, vòng xuyến...)
MIN_REPORT_METERS = 100

# Điểm ghim Maps / geocode: phần quãng đường dọc polyline (0..1)
PIN_PATH_FRACTION = 0.5

# Phát hiện Plus Code ở đầu formatted_address (tránh chọn premise “2QPP+…” làm địa chỉ đường)
_PLUS_CODE_HEAD = re.compile(r"^[A-Z0-9]{2,}\+[A-Z0-9]{2,4}\b", re.IGNORECASE)

# Trong băng này quanh khoảng cách nhỏ nhất: ưu tiên ROOFTOP hơn RANGE_INTERPOLATED.
# Nội suy (RANGE) đôi khi gần ghim hơn vài mét nhưng nhảy nhầm lề / phía đường đối diện.
LOCATION_COMPETE_BAND_M = 3.5

# Khi POI [0] gợi ý một phía đường (chẵn/lẻ) mà không có street_address cùng phía trong băng,
# dùng địa chỉ POI thay vì số nhà đối diện gần hơn về mét.
POI_SIDE_FALLBACK_MAX_M = 45.0

# Tích có hướng pin→địa chỉ so với tiếp tuyến đường; |cross| nhỏ hơn ngưỡng coi là không xác định phía
ROAD_SIDE_CROSS_EPS_M = 1.0


def _vec_meters(origin: tuple[float, float], p: tuple[float, float]) -> tuple[float, float]:
    """Vector origin→p trong mặt phẳng gần đúng (mét)."""
    lat0, lng0 = origin
    m_lat = 111_320.0
    m_lng = 111_320.0 * math.cos(math.radians(lat0))
    return ((p[1] - lng0) * m_lng, (p[0] - lat0) * m_lat)


def _closest_segment_index(points: list[tuple[float, float]], pin: tuple[float, float]) -> int:
    """Chỉ số đoạn points[i]→points[i+1] gần ghim nhất."""
    if len(points) < 2:
        return 0
    best_i = 0
    best_d = float("inf")
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        va = _vec_meters(a, pin)
        vb = _vec_meters(a, b)
        len2 = vb[0] * vb[0] + vb[1] * vb[1]
        if len2 < 1e-12:
            d = math.hypot(va[0], va[1])
        else:
            t = max(0.0, min(1.0, (va[0] * vb[0] + va[1] * vb[1]) / len2))
            proj = (vb[0] * t, vb[1] * t)
            d = math.hypot(va[0] - proj[0], va[1] - proj[1])
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def _unit_tangent_at_pin(
    poly: list[tuple[float, float]],
    pin: tuple[float, float],
) -> tuple[float, float] | None:
    """Vector tiếp tuyến đơn vị (mét) theo chiều polyline tại đoạn gần ghim nhất."""
    if len(poly) < 2:
        return None
    i = _closest_segment_index(poly, pin)
    a, b = poly[i], poly[i + 1]
    vx, vy = _vec_meters(a, b)
    ln = math.hypot(vx, vy)
    if ln < 1e-6:
        return None
    return (vx / ln, vy / ln)


def _road_cross_side_m(
    tangent: tuple[float, float],
    pin: tuple[float, float],
    r: dict,
) -> int:
    """
    Dấu tích có hướng (tiếp tuyến × vector pin→location): +1 / -1 hai phía đường, 0 nếu gần trục.
    """
    loc = (r.get("geometry") or {}).get("location") or {}
    rlat, rlng = loc.get("lat"), loc.get("lng")
    if rlat is None or rlng is None:
        return 0
    vx, vy = _vec_meters(pin, (rlat, rlng))
    c = tangent[0] * vy - tangent[1] * vx
    if c > ROAD_SIDE_CROSS_EPS_M:
        return 1
    if c < -ROAD_SIDE_CROSS_EPS_M:
        return -1
    return 0


def _first_street_number_from_formatted(fa: str) -> int | None:
    """Lấy số nhà đầu tiên từ đoạn đầu formatted_address (hỗ trợ dải 250-252)."""
    first = (fa.split(",")[0] or "").strip()
    m = re.search(r"(\d{1,4})\s*[-–]\s*(\d{1,4})", first)
    if m:
        return int(m.group(1))
    m2 = re.match(r"^\s*(\d+)", first)
    if m2:
        return int(m2.group(1))
    return None


def _parity_hint_from_result0_poi(results: list) -> int | None:
    """0 = ưu tiên số chẵn, 1 = số lẻ; chỉ khi result[0] là POI/establishment có số nhà parse được."""
    if not results:
        return None
    r0 = results[0]
    types = set(r0.get("types") or [])
    if not ("establishment" in types or "point_of_interest" in types):
        return None
    fa = (r0.get("formatted_address") or "").strip()
    if not fa:
        return None
    n = _first_street_number_from_formatted(fa)
    if n is None:
        return None
    return n % 2


def _parity_of_formatted(fa: str) -> int | None:
    n = _first_street_number_from_formatted(fa)
    if n is None:
        return None
    return n % 2


def _opposite_side_hint_suffix(
    results: list,
    lat: float,
    lng: float,
    polyline_points: list[tuple[float, float]] | None,
) -> str:
    """
    Gợi ý ngắn địa chỉ nổi bật phía đường đối diện (so với POI [0]) khi đã xác định được phía bằng polyline.
    """
    if not results or not polyline_points or len(polyline_points) < 2:
        return ""
    pin = (lat, lng)
    tangent = _unit_tangent_at_pin(polyline_points, pin)
    if tangent is None:
        return ""
    r0 = results[0]
    t0 = set(r0.get("types") or [])
    if not ("establishment" in t0 or "point_of_interest" in t0):
        return ""
    s_poi = _road_cross_side_m(tangent, pin, r0)
    if s_poi == 0:
        return ""
    best_fa = ""
    best_d = float("inf")
    for r in results:
        if "street_address" not in (r.get("types") or []):
            continue
        s = _road_cross_side_m(tangent, pin, r)
        if s == 0 or s == s_poi:
            continue
        loc = (r.get("geometry") or {}).get("location") or {}
        rlat, rlng = loc.get("lat"), loc.get("lng")
        if rlat is None or rlng is None:
            continue
        d = _haversine_meters(pin, (rlat, rlng))
        if d < best_d:
            best_d = d
            best_fa = (r.get("formatted_address") or "").strip()
    if not best_fa:
        return ""
    head = (best_fa.split(",")[0] or "").strip()[:100]
    if not head:
        return ""
    return f" · Đối diện đường: {head}"


def _pick_long_formatted_address(
    results: list,
    lat: float,
    lng: float,
    polyline_points: list[tuple[float, float]] | None = None,
) -> str:
    """
    Chọn formatted_address dài từ Geocoding reverse.

    Trong cùng một response có nhiều street_address, ưu tiên geometry.location gần ghim;
    trong băng mét, ưu tiên ROOFTOP trước RANGE_INTERPOLATED.

    Nếu có polyline đoạn đường: dùng tiếp tuyến tại ghim và tích có hướng (pin→địa chỉ)
    để ưu tiên cùng phía đường với POI [0] (khi POI có geometry).

    Nếu result[0] là POI có dải số, thêm lọc chẵn/lẻ và fallback POI như trước khi không có polyline.
    """
    if not results:
        return ""

    pin = (lat, lng)
    tangent = (
        _unit_tangent_at_pin(polyline_points, pin)
        if polyline_points and len(polyline_points) >= 2
        else None
    )

    def _first_seg(addr: str) -> str:
        return (addr.split(",")[0] or "").strip()

    def _dist_to_result(r: dict) -> float:
        loc = (r.get("geometry") or {}).get("location") or {}
        rlat, rlng = loc.get("lat"), loc.get("lng")
        if rlat is None or rlng is None:
            return float("inf")
        return _haversine_meters(pin, (rlat, rlng))

    def _loc_type_rank(r: dict) -> int:
        lt = (r.get("geometry") or {}).get("location_type") or ""
        order = {
            "ROOFTOP": 0,
            "RANGE_INTERPOLATED": 1,
            "GEOMETRIC_CENTER": 2,
        }
        return order.get(lt, 5)

    def _best_among(
        candidates: list[tuple[int, dict]],
        parity_hint: int | None,
        poi0_for_fallback: dict | None,
    ) -> str:
        scored = []
        for orig_i, r in candidates:
            fa = (r.get("formatted_address") or "").strip()
            if not fa:
                continue
            d = _dist_to_result(r)
            scored.append((d, _loc_type_rank(r), orig_i, fa, r))
        if not scored:
            return ""
        d_min = min(x[0] for x in scored)
        band = d_min + LOCATION_COMPETE_BAND_M
        near = [x for x in scored if x[0] <= band]

        if tangent is not None and poi0_for_fallback is not None:
            s_poi = _road_cross_side_m(tangent, pin, poi0_for_fallback)
            if s_poi != 0:
                matched = [
                    x for x in near
                    if _road_cross_side_m(tangent, pin, x[4]) in (0, s_poi)
                ]
                if matched:
                    near = matched

        if parity_hint is not None:
            matched = [
                x for x in near
                if (p := _parity_of_formatted(x[3])) is not None and p == parity_hint
            ]
            if matched:
                near = matched
            elif poi0_for_fallback is not None:
                fa0 = (poi0_for_fallback.get("formatted_address") or "").strip()
                if fa0 and _dist_to_result(poi0_for_fallback) <= POI_SIDE_FALLBACK_MAX_M:
                    return fa0

        near.sort(key=lambda x: (x[1], x[0], x[2]))
        return near[0][3]

    parity_hint = _parity_hint_from_result0_poi(results)
    poi0_fb = None
    if results:
        t0 = set(results[0].get("types") or [])
        if "establishment" in t0 or "point_of_interest" in t0:
            poi0_fb = results[0]

    tier1: list[tuple[int, dict]] = []
    for i, r in enumerate(results):
        types = set(r.get("types") or [])
        if "street_address" not in types:
            continue
        fa = (r.get("formatted_address") or "").strip()
        if not fa:
            continue
        head = _first_seg(fa)
        if "premise" in types and _PLUS_CODE_HEAD.match(head):
            continue
        tier1.append((i, r))

    s = _best_among(tier1, parity_hint, poi0_fb)
    if s:
        return s

    tier2: list[tuple[int, dict]] = []
    for i, r in enumerate(results):
        if "street_address" not in (r.get("types") or []):
            continue
        fa = (r.get("formatted_address") or "").strip()
        if fa:
            tier2.append((i, r))

    s = _best_among(tier2, parity_hint, poi0_fb)
    if s:
        return s

    tier3: list[tuple[int, dict]] = []
    for i, r in enumerate(results):
        types = set(r.get("types") or [])
        if "route" not in types or "street_address" in types:
            continue
        fa = (r.get("formatted_address") or "").strip()
        if fa:
            tier3.append((i, r))

    return _best_among(tier3, parity_hint, poi0_fb)


def check_traffic(origin: str, destination: str) -> dict:
    """
    Kiểm tra giao thông từ origin đến destination.

    Bước 1 — Lấy polyline tổng quan (computeRoutes, không cần field mask traffic, rất rẻ)
    Bước 2 — Sample điểm dọc polyline làm intermediate waypoints
    Bước 3 — Gọi lại computeRoutes với waypoints → nhận traffic từng leg → 1 request duy nhất
    Thêm — Gọi computeRoutes A→B không waypoint → chỉ dùng cho chi tiết tuyến (ít đoạn hơn app Maps)

    Trả về dict: status, duration_normal, duration_traffic, distance,
                 congested_segments, timestamp,
                 route_turn_by_turn, route_legs, route_static_map_png (bytes ảnh hoặc None)
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
    route_static_map_png = _fetch_static_map_png(
        overview.get("encoded_polyline") or "",
        congested_segments,
    )
    congested_segments = _enrich_congested_segments_geocode(congested_segments)

    display_legs = _get_display_navigation_legs(origin, destination)
    if display_legs:
        route_turn_by_turn = _route_turn_by_turn_lines(display_legs)
        route_legs = _build_route_legs(display_legs)
    else:
        route_turn_by_turn = _route_turn_by_turn_lines(legs)
        route_legs = _build_route_legs(legs)

    return {
        "status":                 classify_traffic(total_normal, total_traffic),
        "duration_normal":        total_normal,
        "duration_traffic":       total_traffic,
        "distance":               total_distance,
        "congested_segments":     congested_segments,
        "timestamp":              datetime.now().strftime("%H:%M %d/%m/%Y"),
        "route_turn_by_turn":     route_turn_by_turn,
        "route_legs":             route_legs,
        "route_static_map_png":   route_static_map_png,
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
        "duration_normal":   duration_normal,
        "duration_traffic":  duration_traffic,
        "distance":          distance,
        "polyline_points":   polyline_points,
        "encoded_polyline":  encoded,
    }


def _route_turn_by_turn_lines(legs: list) -> list[str]:
    """Gom toàn bộ bước navigationInstruction từ mọi leg (đánh số 1..n)."""
    lines: list[str] = []
    n = 1
    for leg in legs:
        for t in leg.get("step_instructions") or []:
            lines.append(f"{n}. {t}")
            n += 1
    return lines


def _clip_display_summary(text: str, max_len: int = 500) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[:max_len].rstrip()


def _expand_leg_to_display_segments(leg: dict) -> list[dict]:
    """
    Một leg API dài → nhiều đoạn hiển thị khi có bước NAME_CHANGE (đổi tên đường), gần Maps.
    Thời gian / quãng đường chia theo tỷ lệ số bước; đoạn cuối nhận phần dư làm tròn.
    """
    entries = leg.get("step_entries") or []
    steps_flat = list(leg.get("step_instructions") or [])
    dt = int(leg.get("duration_traffic", 0))
    dn = int(leg.get("duration_normal", 0))
    dm = int(leg.get("distance_m", 0))

    def make_seg(summary: str, sub_steps: list[str], dtt: int, dnn: int, dmm: int) -> dict:
        return {
            "summary": _clip_display_summary(summary),
            "steps": sub_steps,
            "duration_traffic_sec": dtt,
            "duration_normal_sec": dnn,
            "distance_m": dmm,
        }

    if not entries:
        summary = (leg.get("instruction_summary") or "").strip()
        if not summary and steps_flat:
            summary = steps_flat[0]
        if not summary:
            summary = "Đoạn 1"
        return [make_seg(summary, steps_flat, dt, dn, dm)]

    groups: list[list[dict]] = []
    cur: list[dict] = []
    for e in entries:
        m = (e.get("maneuver") or "").strip()
        if m == "NAME_CHANGE" and cur:
            groups.append(cur)
            cur = [e]
        else:
            cur.append(e)
    if cur:
        groups.append(cur)

    if len(groups) <= 1:
        summary = (leg.get("instruction_summary") or "").strip()
        if not summary and steps_flat:
            summary = steps_flat[0]
        if not summary:
            summary = "Đoạn 1"
        return [make_seg(summary, steps_flat, dt, dn, dm)]

    total_n = len(entries)
    out: list[dict] = []
    acc_dt = acc_dn = acc_dm = 0
    for gi, group in enumerate(groups):
        texts = [x["text"] for x in group]
        share = len(group) / total_n if total_n else 1.0
        summary = texts[0] if texts else (leg.get("instruction_summary") or "").strip()
        if gi < len(groups) - 1:
            dtt = int(round(dt * share))
            dnn = int(round(dn * share))
            dmm = int(round(dm * share))
            acc_dt += dtt
            acc_dn += dnn
            acc_dm += dmm
        else:
            dtt = max(0, dt - acc_dt)
            dnn = max(0, dn - acc_dn)
            dmm = max(0, dm - acc_dm)
        out.append(make_seg(summary, texts, dtt, dnn, dmm))
    return out


def _build_route_legs(legs: list) -> list[dict]:
    """
    Mỗi leg Routes → có thể tách nhiều đoạn hiển thị (NAME_CHANGE).
    Nguồn hiển thị: tuyến A→B không waypoint (gần Maps); fallback: legs có waypoint.
    """
    out: list[dict] = []
    for leg in legs:
        segments = _expand_leg_to_display_segments(leg)
        out.extend(segments)
    for i, seg in enumerate(out):
        seg["index"] = i
    return out


def _static_map_segment_polyline_points(seg: dict) -> list[tuple[float, float]]:
    """Điểm để vẽ phủ đoạn tắc/chậm (polyline leg hoặc nối start–end)."""
    pts = seg.get("polyline_points") or []
    if len(pts) >= 2:
        return pts
    return [seg["start"], seg["end"]]


def _static_map_url_with_overlays(
    encoded_polyline: str,
    congested_segments: list,
) -> str:
    """
    Tuyến nền màu xanh + phủ đỏ/vàng trên đoạn congested.
    Static Maps không có lớp traffic thời gian thực như app Maps; đây là mô phỏng từ dữ liệu Routes.
    """
    enc = quote(encoded_polyline, safe="")
    parts: list[str] = [
        "https://maps.googleapis.com/maps/api/staticmap"
        "?size=640x360&scale=2&maptype=roadmap"
        f"&path=weight:4%7Ccolor:0x4285F4FF%7Cenc:{enc}"
    ]
    for seg in congested_segments:
        pts = _static_map_segment_polyline_points(seg)
        if len(pts) < 2:
            continue
        seg_enc = quote(_encode_polyline(pts), safe="")
        col = "0xE53935FF" if seg.get("status") == "red" else "0xF9A825FF"
        parts.append(f"&path=weight:8%7Ccolor:{col}%7Cenc:{seg_enc}")
    parts.append(f"&key={GOOGLE_MAPS_KEY}")
    return "".join(parts)


def _static_map_url_markers_fallback(
    encoded_polyline: str,
    congested_segments: list,
) -> str:
    """Khi URL quá dài: tuyến nền + marker đỏ/vàng tại giữa mỗi đoạn (không vẽ polyline phủ)."""
    enc = quote(encoded_polyline, safe="")
    parts: list[str] = [
        "https://maps.googleapis.com/maps/api/staticmap"
        "?size=640x360&scale=2&maptype=roadmap"
        f"&path=weight:4%7Ccolor:0x4285F4FF%7Cenc:{enc}"
    ]
    for seg in congested_segments:
        slat, slng = seg["start"]
        elat, elng = seg["end"]
        lat = (slat + elat) / 2.0
        lng = (slng + elng) / 2.0
        col = "red" if seg.get("status") == "red" else "yellow"
        parts.append(
            f"&markers=size:mid%7Ccolor:{col}%7C{lat:.6f},{lng:.6f}"
        )
    parts.append(f"&key={GOOGLE_MAPS_KEY}")
    return "".join(parts)


def _static_map_route_url(
    encoded_polyline: str,
    congested_segments: list | None = None,
) -> str:
    """URL Static Maps — chỉ dùng server-side (_fetch_static_map_png); không gửi URL cho user."""
    if not encoded_polyline or not GOOGLE_MAPS_KEY:
        return ""
    segs = congested_segments or []
    if not segs:
        enc = quote(encoded_polyline, safe="")
        return (
            "https://maps.googleapis.com/maps/api/staticmap"
            "?size=640x360&scale=2&maptype=roadmap"
            "&path=weight:4%7Ccolor:0x4285F4FF%7Cenc:"
            f"{enc}&key={GOOGLE_MAPS_KEY}"
        )
    url = _static_map_url_with_overlays(encoded_polyline, segs)
    if len(url) <= STATIC_MAP_MAX_URL_LEN:
        return url
    url_fb = _static_map_url_markers_fallback(encoded_polyline, segs)
    if len(url_fb) <= STATIC_MAP_MAX_URL_LEN:
        logger.info("Static Maps: URL phủ đoạn quá dài → dùng marker thay polyline phủ.")
        return url_fb
    logger.warning("Static Maps: URL vẫn quá dài sau fallback → chỉ tuyến nền.")
    enc = quote(encoded_polyline, safe="")
    return (
        "https://maps.googleapis.com/maps/api/staticmap"
        "?size=640x360&scale=2&maptype=roadmap"
        "&path=weight:4%7Ccolor:0x4285F4FF%7Cenc:"
        f"{enc}&key={GOOGLE_MAPS_KEY}"
    )


def _fetch_static_map_png(
    encoded_polyline: str,
    congested_segments: list | None = None,
) -> bytes | None:
    """GET ảnh Static Maps trên server (1 request Static Maps / lần gọi khi thành công)."""
    url = _static_map_route_url(encoded_polyline, congested_segments)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if "image" not in ct:
            logger.warning("Static Maps không trả ảnh: Content-Type=%s", ct)
            return None
        return r.content
    except requests.RequestException as e:
        logger.warning("Static Maps tải ảnh lỗi: %s", e)
        return None


# ── Bước 3: Legs với traffic ───────────────────────────────────────────────────

def _parse_route_legs_payload(legs_raw: list) -> list:
    """Chuẩn hoá JSON legs từ Routes API → list dict nội bộ."""
    legs: list = []
    for i, leg in enumerate(legs_raw):
        duration_traffic = _parse_duration(leg.get("duration", "0s"))
        duration_normal  = _parse_duration(leg.get("staticDuration", "0s"))
        distance_m       = leg.get("distanceMeters", 0)

        start = leg.get("startLocation", {}).get("latLng", {})
        end   = leg.get("endLocation",   {}).get("latLng", {})
        instruction_summary = _extract_leg_instruction(leg)

        enc_leg = leg.get("polyline", {}).get("encodedPolyline", "")
        leg_poly = _decode_polyline(enc_leg) if enc_leg else []

        step_entries: list[dict] = []
        step_instructions: list[str] = []
        for step in leg.get("steps", []):
            nav = step.get("navigationInstruction", {})
            text = _normalize_instruction(nav.get("instructions", ""))
            if not text:
                continue
            maneuver = (nav.get("maneuver") or "").strip()
            step_entries.append({"text": text, "maneuver": maneuver})
            step_instructions.append(text)

        legs.append({
            "index":               i,
            "duration_normal":     duration_normal,
            "duration_traffic":    duration_traffic,
            "distance_m":          distance_m,
            "start":               (start.get("latitude", 0), start.get("longitude", 0)),
            "end":                 (end.get("latitude", 0),   end.get("longitude", 0)),
            "instruction_summary": instruction_summary,
            "polyline_points":     leg_poly,
            "step_entries":        step_entries,
            "step_instructions":   step_instructions,
        })
    return legs


def _get_display_navigation_legs(origin: str, destination: str) -> list | None:
    """
    Một request computeRoutes A→B không waypoint — ít leg hơn, gần UI Maps.
    Chỉ dùng cho route_legs / route_turn_by_turn; không dùng cho tắc nghẽn.
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
        "routes.legs.duration,routes.legs.staticDuration,"
        "routes.legs.distanceMeters,routes.legs.startLocation,routes.legs.endLocation,"
        "routes.legs.polyline,"
        "routes.legs.steps,"
        "routes.legs.steps.navigationInstruction.instructions,"
        "routes.legs.steps.navigationInstruction.maneuver"
    )
    try:
        resp = requests.post(ROUTES_URL, json=body, headers=headers, timeout=20)
        _raise_for_routes_error(resp)
        data = resp.json()
        if not data.get("routes"):
            return None
        legs_raw = data["routes"][0].get("legs", [])
        legs = _parse_route_legs_payload(legs_raw)
        logger.info(f"Chi tiết tuyến (hiển thị): {len(legs)} leg(s), không waypoint")
        return legs
    except Exception as e:
        logger.warning("Không lấy được tuyến chỉ đường hiển thị (fallback waypoint): %s", e)
        return None


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
        "routes.legs.steps,"
        "routes.legs.steps.navigationInstruction.instructions,"
        "routes.legs.steps.navigationInstruction.maneuver"
    )

    resp = requests.post(ROUTES_URL, json=body, headers=headers, timeout=20)
    _raise_for_routes_error(resp)
    data = resp.json()

    if not data.get("routes"):
        raise Exception("Không nhận được dữ liệu legs từ Routes API.")

    legs_raw = data["routes"][0].get("legs", [])
    legs = _parse_route_legs_payload(legs_raw)
    logger.info(f"Legs (waypoint): {len(legs)} đoạn nhận được từ Routes API")
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
        poly_for_tangent = pts if len(pts) >= 2 else [start, end]
        road_name = _reverse_geocode_road_name(pin[0], pin[1], poly_for_tangent)
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


def _reverse_geocode_road_name(
    lat: float,
    lng: float,
    polyline_points: list[tuple[float, float]] | None = None,
) -> str:
    """
    Reverse geocoding tại một điểm.
    polyline_points: polyline đoạn tắc (hoặc [start,end]) để xác định phía đường so với POI.

    Ưu tiên: formatted_address đầy đủ (street_address) → số nhà + tên đường → POI / tên đường ngắn.
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

    results = data.get("results", [])

    # ── LOG: Toàn bộ raw response để debug ────────────────────────────────────
    logger.debug("=== GEOCODING RAW (%.6f, %.6f) — %d result(s) ===", lat, lng, len(results))
    for i, result in enumerate(results):
        logger.debug(
            "  [%d] types=%-55s | formatted_address=%s",
            i,
            str(result.get("types", [])),
            result.get("formatted_address", ""),
        )
        for comp in result.get("address_components", []):
            logger.debug(
                "       component: %-35s types=%s",
                repr(comp.get("long_name", "")),
                comp.get("types", []),
            )
    # ──────────────────────────────────────────────────────────────────────────

    street_number = ""
    route_name    = ""
    poi_name      = ""

    for result in results:
        components = result.get("address_components", [])
        types      = result.get("types", [])

        # Thu thập POI / landmark từ result-level types
        is_poi = any(t in types for t in (
            "point_of_interest", "establishment",
            "premise", "natural_feature",
        ))
        if is_poi and not poi_name:
            candidate = result.get("name", "").strip()
            if not candidate:
                candidate = result.get("formatted_address", "").split(",")[0].strip()
            if candidate:
                poi_name = candidate
                logger.debug("  → POI picked: %r (từ result[types=%s])", poi_name, types)

        # Thu thập số nhà + tên đường từ address_components
        for comp in components:
            ctypes = comp.get("types", [])
            name   = (comp.get("long_name") or "").strip()
            if not name:
                continue
            if "street_number" in ctypes and not street_number:
                street_number = name
                logger.debug("  → street_number picked: %r", street_number)
            if "route" in ctypes and not route_name:
                route_name = name
                logger.debug("  → route picked: %r", route_name)

        # Dừng sớm nếu đã đủ thông tin
        if street_number and route_name:
            break

    # ── LOG: Kết quả cuối cùng ────────────────────────────────────────────────
    logger.debug(
        "  → FINAL: street_number=%r  route=%r  poi=%r",
        street_number, route_name, poi_name,
    )
    # ──────────────────────────────────────────────────────────────────────────

    long_formatted = _pick_long_formatted_address(results, lat, lng, polyline_points)
    if long_formatted:
        logger.debug("  → LONG formatted_address: %r", long_formatted)
        suf = _opposite_side_hint_suffix(results, lat, lng, polyline_points)
        return long_formatted + suf

    # Ráp kết quả ngắn theo thứ tự ưu tiên
    if street_number and route_name:
        return f"{street_number} {route_name}"      # "145 Đường Láng"

    if route_name and poi_name:
        return f"{route_name} (gần {poi_name})"     # "Đường Láng (gần Vincom)"

    if route_name:
        return route_name                            # fallback: tên đường đơn thuần

    if poi_name:
        return f"gần {poi_name}"

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


def _extract_leg_instruction(leg: dict, max_steps: int = 2, max_len: int = 420) -> str:
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


def _encode_polyline_signed(n: int) -> str:
    n = n << 1 if n >= 0 else ~n
    chunks: list[str] = []
    while n >= 0x20:
        chunks.append(chr((0x20 | (n & 0x1F)) + 63))
        n >>= 5
    chunks.append(chr(n + 63))
    return "".join(chunks)


def _encode_polyline(points: list[tuple[float, float]]) -> str:
    """Encode list (lat, lng) → Google Encoded Polyline (dùng cho Static Maps path)."""
    if not points:
        return ""
    out: list[str] = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        ilat = int(round(lat * 1e5))
        ilng = int(round(lng * 1e5))
        d_lat = ilat - prev_lat
        d_lng = ilng - prev_lng
        prev_lat, prev_lng = ilat, ilng
        out.append(_encode_polyline_signed(d_lat))
        out.append(_encode_polyline_signed(d_lng))
    return "".join(out)


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
    if ratio <= 1.0:
        return "green"
    elif ratio < 1.4:
        return "yellow"
    else:
        return "red"
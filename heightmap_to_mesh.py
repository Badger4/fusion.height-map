"""
Heightmap -> MeshBody для Fusion 360 (з інтерактивним діалогом)
=================================================================
Що робить:
  1. Обираєш файл зображення (8-бітні та 16-бітні карти висот: PNG, TIFF, BMP, JPG).
  2. Супер-прискорений Binary STL експорт та векторизація NumPy (з замиканням на чистий Python-fallback).
  3. Динамічний інформаційний блок у діалозі (роздільність, полігони, точний час обчислення).
  4. Окремий твердотільний плоский об'єкт (Solid BRep Body) з фасками/скругленнями та монтажними отворами:
     - Отвори під гвинти M3, M4, M5, M6 у 4 кутах з налаштовуваним відступом.
     - Автоматичні фаски (Chamfer) або скручування (Fillet) на краях основи.
  5. Опція "Плавний згасаючий край" (Vignette Fade) з галочкою — м'який вихід рельєфу до Z=0 по периметру.
  6. Вибір початку координат (Origin Alignment):
     - Центр у точці (0, 0, 0)
     - Лівий нижній кут у (0, 0, 0)
     - Верхня точка рельєфу Z = 0
  7. Режим «Літофанія» (Lithophane Preset) для 3D-друку просвітних картин.
  8. Збереження та відновлення останніх налаштувань через JSON-конфіг.
  9. Автоматичний авто-інсталятор Pillow в один клік.

Запуск: Scripts and Add-Ins -> вибрати файл -> Run.
"""

import adsk.core, adsk.fusion, adsk.cam, traceback
import os
import sys
import math
import array
import json
import struct
import tempfile
import subprocess
import time
import statistics
from collections import deque

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None
    HAS_NUMPY = False

_app = None
_ui = None
handlers = []
_is_updating_aspect = False
_img_aspect_ratio = 1.0

CMD_ID = 'heightmapToMeshCmd'
CMD_NAME = 'Heightmap to Mesh'
PANEL_ID = 'SolidCreatePanel'

MAX_VERTICES_WARNING = 600000
PARAMS_FILE = os.path.join(tempfile.gettempdir(), 'heightmap_fusion_params.json')


# =====================================================================
# Збереження та підвантаження параметрів (JSON)
# =====================================================================

def save_last_params(params):
    try:
        with open(PARAMS_FILE, 'w', encoding='utf-8') as f:
            json.dump(params, f, ensure_ascii=False, indent=2)
    except:
        pass


def load_last_params():
    try:
        if os.path.exists(PARAMS_FILE):
            with open(PARAMS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return {}


# =====================================================================
# Автоматична перевірка та встановлення Pillow
# =====================================================================

def ensure_pillow(ui):
    try:
        import PIL
        from PIL import Image, ImageFilter
        return True
    except ImportError:
        res = ui.messageBox(
            "Модуль Pillow (PIL) не знайдено у вбудованому Python-середовищі Fusion 360.\n\n"
            "Бажаєте встановити його автоматично прямо зараз?",
            "Авто-встановлення Pillow",
            adsk.core.MessageBoxButtonTypes.YesNoButtonType,
            adsk.core.MessageBoxIconTypes.QuestionIconType
        )
        if res == adsk.core.DialogResults.DialogYes:
            progress = ui.createProgressDialog()
            progress.isCancelButtonShown = False
            progress.show("Встановлення Pillow", "Перевірка pip...", 0, 100, 1)
            try:
                # 1. Ініціалізація ensurepip (якщо pip відсутній)
                progress.progressValue = 20
                progress.message = "Ініціалізація пакета pip..."
                proc_pip = subprocess.Popen(
                    [sys.executable, "-m", "ensurepip", "--default-pip"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                while proc_pip.poll() is None:
                    adsk.doEvents()
                    time.sleep(0.05)

                # 2. Встановлення Pillow
                progress.progressValue = 50
                progress.message = "Завантаження та встановлення Pillow..."
                proc_inst = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "Pillow", "--disable-pip-version-check"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                while proc_inst.poll() is None:
                    adsk.doEvents()
                    time.sleep(0.05)

                out, err = proc_inst.communicate()
                progress.hide()

                if proc_inst.returncode == 0:
                    ui.messageBox("Pillow успішно встановлено!")
                    return True
                else:
                    err_text = err.decode('utf-8', errors='ignore') if err else 'Невідома помилка'
                    ui.messageBox(
                        f"Не вдалося встановити Pillow автоматично.\n\n"
                        f"Помилка: {err_text}\n\n"
                        f"Виконайте в PowerShell:\n"
                        f'& "{sys.executable}" -m pip install Pillow'
                    )
                    return False
            except Exception:
                progress.hide()
                ui.messageBox(f"Не вдалося встановити Pillow:\n{traceback.format_exc()}")
                return False
        return False


# =====================================================================
# Геометричні допоміжні функції
# =====================================================================

def make_regular_polygon(cx, cy, sides, radius):
    verts = []
    for i in range(sides):
        angle = -math.pi / 2.0 + i * (2.0 * math.pi / sides)
        verts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return verts


def point_in_polygon(px, py, poly):
    inside = False
    n = len(poly)
    if n < 3:
        return False
    p1x, p1y = poly[0]
    for i in range(1, n + 1):
        p2x, p2y = poly[i % n]
        if min(p1y, p2y) < py <= max(p1y, p2y):
            if px <= max(p1x, p2x):
                if p1y != p2y:
                    xinters = (py - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                if p1x == p2x or px <= xinters:
                    inside = not inside
        p1x, p1y = p2x, p2y
    return inside


def get_content_bbox(img, only_alpha=True, threshold=32):
    try:
        if img.mode in ('RGBA', 'LA') or ('transparency' in img.info):
            alpha = img.convert('RGBA').split()[-1]
            extrema = alpha.getextrema()
            if extrema and extrema[0] < threshold:
                mask = alpha.point(lambda p: 255 if p >= threshold else 0)
                bbox = mask.getbbox()
                if bbox:
                    return bbox

        if not only_alpha:
            img_rgb = img.convert('RGB')
            w, h = img_rgb.size
            if HAS_NUMPY:
                arr = np.asarray(img_rgb, dtype=np.float32)
                corners = [arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]]
                bg = np.median(corners, axis=0)
                color_tol = max(25.0, float(threshold) * 1.732)
                diff = arr - bg
                dist_sq = np.sum(diff * diff, axis=-1)
                content = dist_sq > (color_tol ** 2)
                y_idx, x_idx = np.where(content)
                if len(y_idx) > 0:
                    return (int(x_idx.min()), int(y_idx.min()), int(x_idx.max()) + 1, int(y_idx.max()) + 1)
            else:
                img_gray = img.convert('L')
                pixels = img_gray.tobytes()
                corners = [pixels[0], pixels[w - 1], pixels[(h - 1) * w], pixels[w * h - 1]]
                avg_corner = statistics.median(corners)
                tol = max(25, threshold)
                mask = img_gray.point(lambda p: 255 if abs(p - avg_corner) > tol else 0)
                return mask.getbbox()
        return None
    except:
        return None


def get_aspect_ratio_value(aspect_str, img_aspect):
    if aspect_str == '1:1 (Квадрат / Коло)':
        return 1.0
    elif aspect_str == '4:3 (Стандарт)':
        return 4.0 / 3.0
    elif aspect_str == '16:9 (Широкий)':
        return 16.0 / 9.0
    elif aspect_str == '3:2 (Фото)':
        return 3.0 / 2.0
    elif aspect_str == 'За пропорціями картинки (Оригінал)':
        return img_aspect if img_aspect > 0 else 1.0
    return None  # 'Довільне (Вручну)'


def fit_image_to_grid(img, target_cols, target_rows, fit_mode='Вписати зі збереженням пропорцій (Fit)', bg_color=None):
    from PIL import Image
    resample_filter = getattr(Image, 'Resampling', Image).BILINEAR
    if fit_mode == 'Розтягнути по формі (Stretch)':
        return img.resize((target_cols, target_rows), resample_filter)

    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0 or target_cols <= 0 or target_rows <= 0:
        return img.resize((target_cols, target_rows), resample_filter)

    src_aspect = float(src_w) / float(src_h)
    tgt_aspect = float(target_cols) / float(target_rows)

    if 'Вписати' in fit_mode:
        if src_aspect > tgt_aspect:
            new_w = target_cols
            new_h = max(1, int(round(target_cols / src_aspect)))
        else:
            new_h = target_rows
            new_w = max(1, int(round(target_rows * src_aspect)))

        scaled = img.resize((new_w, new_h), resample_filter)
        if bg_color is None:
            if img.mode in ('RGBA', 'LA') or ('transparency' in img.info):
                bg_color = (0, 0, 0, 0)
            elif img.mode == 'RGB':
                c = [img.getpixel((0, 0)), img.getpixel((src_w - 1, 0)), img.getpixel((0, src_h - 1)), img.getpixel((src_w - 1, src_h - 1))]
                bg_color = (int(statistics.median([p[0] for p in c])), int(statistics.median([p[1] for p in c])), int(statistics.median([p[2] for p in c])))
            elif img.mode in ('L', 'I', 'I;16', 'I;16L', 'I;16B'):
                c = [img.getpixel((0, 0)), img.getpixel((src_w - 1, 0)), img.getpixel((0, src_h - 1)), img.getpixel((src_w - 1, src_h - 1))]
                bg_color = int(statistics.median(c))
            else:
                bg_color = 0

        canvas = Image.new(img.mode, (target_cols, target_rows), bg_color)
        off_x = (target_cols - new_w) // 2
        off_y = (target_rows - new_h) // 2
        canvas.paste(scaled, (off_x, off_y))
        return canvas

    elif 'Заповнити' in fit_mode:
        if src_aspect > tgt_aspect:
            new_h = target_rows
            new_w = max(1, int(round(target_rows * src_aspect)))
        else:
            new_w = target_cols
            new_h = max(1, int(round(target_cols / src_aspect)))

        scaled = img.resize((new_w, new_h), resample_filter)
        off_x = max(0, (new_w - target_cols) // 2)
        off_y = max(0, (new_h - target_rows) // 2)
        return scaled.crop((off_x, off_y, off_x + target_cols, off_y + target_rows))

    return img.resize((target_cols, target_rows), resample_filter)


def detect_outer_mask(grid_cols, grid_rows, alpha_bytes=None, rgb_arr=None, rgb_bytes=None, raw_pixels=None, raw_arr=None, is_16bit=False, threshold=32, fill_holes=True):
    total = grid_cols * grid_rows

    use_alpha = False
    if alpha_bytes is not None:
        sample_step = max(1, len(alpha_bytes) // 500)
        if any(alpha_bytes[i] < threshold for i in range(0, len(alpha_bytes), sample_step)):
            use_alpha = True

    if use_alpha:
        if not fill_holes:
            return bytearray(1 if alpha_bytes[i] >= threshold else 0 for i in range(total))
        is_bg = lambda idx: alpha_bytes[idx] < threshold
    elif raw_arr is not None and HAS_NUMPY:
        corners = [raw_arr[0, 0], raw_arr[0, -1], raw_arr[-1, 0], raw_arr[-1, -1]]
        bg_val = float(np.median(corners))
        tol = float(threshold * 256.0) if is_16bit else max(25.0, float(threshold))

        corner_dists = np.abs(np.array(corners, dtype=np.float32) - bg_val)
        has_uniform_bg = (np.sum(corner_dists <= tol) >= 3)
        if not has_uniform_bg:
            return bytearray(b'\x01' * total)

        diff = np.abs(raw_arr.astype(np.float32) - bg_val)
        is_bg_mat = diff <= tol

        if not fill_holes:
            return bytearray((~is_bg_mat).astype(np.uint8).ravel().tobytes())

        visited = np.zeros((grid_rows, grid_cols), dtype=bool)
        queue = deque()
        for x in range(grid_cols):
            if is_bg_mat[0, x] and not visited[0, x]:
                visited[0, x] = True; queue.append((0, x))
            if is_bg_mat[grid_rows - 1, x] and not visited[grid_rows - 1, x]:
                visited[grid_rows - 1, x] = True; queue.append((grid_rows - 1, x))
        for y in range(grid_rows):
            if is_bg_mat[y, 0] and not visited[y, 0]:
                visited[y, 0] = True; queue.append((y, 0))
            if is_bg_mat[y, grid_cols - 1] and not visited[y, grid_cols - 1]:
                visited[y, grid_cols - 1] = True; queue.append((y, grid_cols - 1))

        while queue:
            cy, cx = queue.popleft()
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < grid_rows and 0 <= nx < grid_cols:
                    if not visited[ny, nx] and is_bg_mat[ny, nx]:
                        visited[ny, nx] = True; queue.append((ny, nx))

        is_object = ~visited
        return bytearray(is_object.astype(np.uint8).ravel().tobytes())
    elif rgb_arr is not None and HAS_NUMPY:
        sample_pts = [
            (0, 0), (grid_rows - 1, 0), (0, grid_cols - 1), (grid_rows - 1, grid_cols - 1),
            (grid_rows // 2, 0), (grid_rows // 2, grid_cols - 1),
            (0, grid_cols // 2), (grid_rows - 1, grid_cols // 2)
        ]
        sampled_colors = [rgb_arr[r, c] for (r, c) in sample_pts]
        bg_color = np.median(sampled_colors, axis=0)

        color_tol = max(25.0, float(threshold) * 1.732)
        corners = [rgb_arr[0, 0], rgb_arr[-1, 0], rgb_arr[0, -1], rgb_arr[-1, -1]]
        corner_dists = np.sqrt(np.sum((corners - bg_color) ** 2, axis=-1))
        has_uniform_bg = (np.sum(corner_dists <= color_tol) >= 3)

        if not has_uniform_bg:
            return bytearray(b'\x01' * total)

        diff = rgb_arr.astype(np.float32) - bg_color
        dist_sq = np.sum(diff * diff, axis=-1)
        is_bg_mat = dist_sq <= (color_tol ** 2)

        if not fill_holes:
            return bytearray(0 if is_bg_mat.ravel()[i] else 1 for i in range(total))

        is_bg_flat = is_bg_mat.ravel()
        is_bg = lambda idx: bool(is_bg_flat[idx])
    elif rgb_bytes is not None:
        sample_indices = [
            0, (grid_cols - 1),
            (grid_rows - 1) * grid_cols, total - 1,
            (grid_rows // 2) * grid_cols, (grid_rows // 2) * grid_cols + (grid_cols - 1),
            grid_cols // 2, (grid_rows - 1) * grid_cols + (grid_cols // 2)
        ]
        r_vals = [rgb_bytes[i * 3] for i in sample_indices]
        g_vals = [rgb_bytes[i * 3 + 1] for i in sample_indices]
        b_vals = [rgb_bytes[i * 3 + 2] for i in sample_indices]
        bg_r = statistics.median(r_vals)
        bg_g = statistics.median(g_vals)
        bg_b = statistics.median(b_vals)

        color_tol_sq = (max(25.0, float(threshold) * 1.732)) ** 2
        corner_indices = [0, grid_cols - 1, (grid_rows - 1) * grid_cols, total - 1]
        matches = 0
        for ci in corner_indices:
            dr = rgb_bytes[ci * 3] - bg_r
            dg = rgb_bytes[ci * 3 + 1] - bg_g
            db = rgb_bytes[ci * 3 + 2] - bg_b
            if (dr * dr + dg * dg + db * db) <= color_tol_sq:
                matches += 1

        if matches < 3:
            return bytearray(b'\x01' * total)

        if not fill_holes:
            res = bytearray(total)
            for i in range(total):
                dr = rgb_bytes[i * 3] - bg_r
                dg = rgb_bytes[i * 3 + 1] - bg_g
                db = rgb_bytes[i * 3 + 2] - bg_b
                if (dr * dr + dg * dg + db * db) > color_tol_sq:
                    res[i] = 1
            return res

        def is_bg(idx):
            dr = rgb_bytes[idx * 3] - bg_r
            dg = rgb_bytes[idx * 3 + 1] - bg_g
            db = rgb_bytes[idx * 3 + 2] - bg_b
            return (dr * dr + dg * dg + db * db) <= color_tol_sq
    else:
        corner_indices = [0, grid_cols - 1, (grid_rows - 1) * grid_cols, total - 1]
        if is_16bit and raw_pixels is not None:
            if isinstance(raw_pixels, (bytes, bytearray)):
                if len(raw_pixels) >= total * 4:
                    val_func = lambda i: struct.unpack_from('<I', raw_pixels, i * 4)[0]
                else:
                    val_func = lambda i: struct.unpack_from('<H', raw_pixels, i * 2)[0]
            else:
                val_func = lambda i: raw_pixels[i]
            corner_vals = [val_func(i) for i in corner_indices]
            bg_val = statistics.median(corner_vals)
            tol = threshold * 256
            if not fill_holes:
                return bytearray(1 if abs(val_func(i) - bg_val) > tol else 0 for i in range(total))
            is_bg = lambda idx: abs(val_func(idx) - bg_val) <= tol
        elif raw_pixels is not None:
            corner_vals = [raw_pixels[i] for i in corner_indices]
            bg_val = statistics.median(corner_vals)
            tol = threshold
            if not fill_holes:
                return bytearray(1 if abs(raw_pixels[i] - bg_val) > tol else 0 for i in range(total))
            is_bg = lambda idx: abs(raw_pixels[idx] - bg_val) <= tol
        else:
            return bytearray(b'\x01' * total)

    visited = bytearray(total)
    queue = deque()
    q_append = queue.append
    w_minus_1 = grid_cols - 1
    h_minus_1 = grid_rows - 1

    for x in range(grid_cols):
        if not visited[x] and is_bg(x):
            visited[x] = 1
            q_append(x)
        idx_b = h_minus_1 * grid_cols + x
        if not visited[idx_b] and is_bg(idx_b):
            visited[idx_b] = 1
            q_append(idx_b)

    for y in range(1, h_minus_1):
        idx_l = y * grid_cols
        if not visited[idx_l] and is_bg(idx_l):
            visited[idx_l] = 1
            q_append(idx_l)
        idx_r = y * grid_cols + w_minus_1
        if not visited[idx_r] and is_bg(idx_r):
            visited[idx_r] = 1
            q_append(idx_r)

    q_pop = queue.popleft
    while queue:
        idx = q_pop()
        x = idx % grid_cols
        if x > 0:
            n = idx - 1
            if not visited[n] and is_bg(n):
                visited[n] = 1
                q_append(n)
        if x < w_minus_1:
            n = idx + 1
            if not visited[n] and is_bg(n):
                visited[n] = 1
                q_append(n)
        if idx >= grid_cols:
            n = idx - grid_cols
            if not visited[n] and is_bg(n):
                visited[n] = 1
                q_append(n)
        if idx < h_minus_1 * grid_cols:
            n = idx + grid_cols
            if not visited[n] and is_bg(n):
                visited[n] = 1
                q_append(n)

    is_object = bytearray(1 if not visited[i] else 0 for i in range(total))
    return is_object


def chain_boundary_edges(boundary_edges, vertices):
    from collections import defaultdict
    adj = defaultdict(list)
    for a, b in boundary_edges:
        adj[a].append(b)

    visited_edges = set()
    loops = []

    for start_node in list(adj.keys()):
        for next_node in list(adj[start_node]):
            if (start_node, next_node) not in visited_edges:
                loop = [start_node]
                visited_edges.add((start_node, next_node))
                curr = next_node

                while curr != start_node:
                    loop.append(curr)
                    next_candidates = [nbr for nbr in adj[curr] if (curr, nbr) not in visited_edges]
                    if not next_candidates:
                        break
                    next_node = next_candidates[0]
                    visited_edges.add((curr, next_node))
                    curr = next_node

                if curr == start_node and len(loop) >= 3:
                    loop_pts = [(vertices[v][0], vertices[v][1]) for v in loop]
                    loops.append(loop_pts)

    return loops


def rdp_simplify(points, epsilon=0.2):
    if len(points) < 3:
        return points

    x0, y0 = points[0]
    x1, y1 = points[-1]
    dx = x1 - x0
    dy = y1 - y0
    line_len = math.hypot(dx, dy)

    dmax = 0.0
    index = 0
    end = len(points) - 1

    for i in range(1, end):
        px, py = points[i]
        if line_len > 1e-9:
            d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / line_len
        else:
            d = math.hypot(px - x0, py - y0)
        if d > dmax:
            index = i
            dmax = d

    if dmax > epsilon:
        rec1 = rdp_simplify(points[:index + 1], epsilon)
        rec2 = rdp_simplify(points[index:], epsilon)
        return rec1[:-1] + rec2
    else:
        return [points[0], points[-1]]


def simplify_closed_loop(loop, epsilon=0.2):
    n = len(loop)
    if n < 6:
        return loop
    mid = n // 2
    part1 = rdp_simplify(loop[:mid + 1], epsilon)
    part2 = rdp_simplify(loop[mid:] + [loop[0]], epsilon)
    return part1[:-1] + part2[:-1]


def gaussian_blur_2d(arr, sigma=1.0):
    """Швидке та якісне роздільне 2D-гаусове згладжування для NumPy масивів."""
    if sigma <= 0.0:
        return arr
    radius = max(1, int(math.ceil(2.5 * sigma)))
    x = np.arange(-radius, radius + 1)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    kernel = kernel.astype(arr.dtype)

    pad_w = [(0, 0), (radius, radius)]
    padded_r = np.pad(arr, pad_w, mode='edge')
    res_r = np.zeros_like(arr)
    for i, k in enumerate(kernel):
        res_r += k * padded_r[:, i:i + arr.shape[1]]

    pad_h = [(radius, radius), (0, 0)]
    padded_c = np.pad(res_r, pad_h, mode='edge')
    res = np.zeros_like(arr)
    for j, k in enumerate(kernel):
        res += k * padded_c[j:j + arr.shape[0], :]

    return res


def smooth_grid_python(heights, cols, rows, passes=1):
    """Зважене плаваюче 3x3 згладжування висот для чистого Python-fallback."""
    curr = list(heights)
    for _ in range(passes):
        nxt = list(curr)
        for y in range(rows):
            row = y * cols
            y_prev = max(0, y - 1) * cols
            y_next = min(rows - 1, y + 1) * cols
            for x in range(cols):
                x_prev = max(0, x - 1)
                x_next = min(cols - 1, x + 1)
                val = (
                    curr[y_prev + x_prev] + curr[y_prev + x] * 2.0 + curr[y_prev + x_next] +
                    curr[row + x_prev] * 2.0 + curr[row + x] * 4.0 + curr[row + x_next] * 2.0 +
                    curr[y_next + x_prev] + curr[y_next + x] * 2.0 + curr[y_next + x_next]
                ) / 16.0
                nxt[row + x] = val
        curr = nxt
    return curr


# =====================================================================
# Швидкий Binary STL експортер
# =====================================================================

def write_stl_binary(path, vertices, triangles, progress=None):
    """Високоефективний запис у бінарний STL з нормалями векторів."""
    n_triangles = len(triangles)
    chunk_size = 50000

    with open(path, 'wb') as f:
        header = b'Binary STL generated by Fusion 360 Heightmap Add-In'.ljust(80, b'\x00')
        f.write(header)
        f.write(struct.pack('<I', n_triangles))

        if HAS_NUMPY:
            v_arr = np.ascontiguousarray(vertices, dtype=np.float32)
            t_arr = np.ascontiguousarray(triangles, dtype=np.int32)

            tri_dtype = np.dtype([
                ('normal', 'f4', (3,)),
                ('v1', 'f4', (3,)),
                ('v2', 'f4', (3,)),
                ('v3', 'f4', (3,)),
                ('attr', 'u2')
            ])
            data = np.zeros(n_triangles, dtype=tri_dtype)
            data['v1'] = v_arr[t_arr[:, 0]]
            data['v2'] = v_arr[t_arr[:, 1]]
            data['v3'] = v_arr[t_arr[:, 2]]

            for i in range(0, n_triangles, chunk_size):
                if progress and progress.wasCancelled:
                    return
                adsk.doEvents()
                f.write(data[i:i + chunk_size].tobytes())
        else:
            for i in range(0, n_triangles, chunk_size):
                if progress and progress.wasCancelled:
                    return
                adsk.doEvents()
                chunk_buf = bytearray()
                for a, b, c in triangles[i:i + chunk_size]:
                    v1 = vertices[a]
                    v2 = vertices[b]
                    v3 = vertices[c]
                    ax, ay, az = v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2]
                    bx, by, bz = v3[0] - v1[0], v3[1] - v1[1], v3[2] - v1[2]
                    nx = ay * bz - az * by
                    ny = az * bx - ax * bz
                    nz = ax * by - ay * bx
                    nl = math.hypot(nx, math.hypot(ny, nz))
                    if nl > 1e-9:
                        nx /= nl; ny /= nl; nz /= nl
                    else:
                        nx, ny, nz = 0.0, 0.0, 1.0
                    chunk_buf.extend(struct.pack('<12fH', nx, ny, nz, v1[0], v1[1], v1[2], v2[0], v2[1], v2[2], v3[0], v3[1], v3[2], 0))
                f.write(chunk_buf)


# =====================================================================
# add-in lifecycle
# =====================================================================

def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        if not ensure_pillow(_ui):
            return

        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME,
                'Створює MeshBody-рельєф із зображення з генерацією BRep-основи, оттворами M3-M6, згасанням та CAM-інструментами'
            )

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)

        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        control = panel.controls.itemById(CMD_ID)
        if not control:
            control = panel.controls.addCommand(cmd_def)
            control.isPromotedByDefault = True
            control.isPromoted = True

        cmd_def.execute()
        adsk.autoTerminate(False)
    except:
        if _ui:
            _ui.messageBox('Помилка запуску:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            control = panel.controls.itemById(CMD_ID)
            if control:
                control.deleteMe()
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
    except:
        if _ui:
            _ui.messageBox('Помилка зупинки:\n{}'.format(traceback.format_exc()))


# =====================================================================
# Побудова діалогу
# =====================================================================

class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            inputs = cmd.commandInputs
            saved = load_last_params()

            # ---- Інформаційний блок ----
            info_text = inputs.addTextBoxCommandInput('infoBox', 'Інформація про сітку', 'Оберіть файл та налаштуйте розміри.', 2, True)
            info_text.isFullWidth = True

            # ---- Зображення ----
            init_img_path = saved.get('imagePath', '')
            img_input = inputs.addStringValueInput('imagePath', 'Файл зображення', init_img_path)
            img_input.isReadOnly = True
            inputs.addBoolValueInput('browseBtn', 'Обрати зображення...', False, '', False)
            auto_crop = inputs.addBoolValueInput('autoCrop', 'Обрізати поля навколо об\'єкта (Alpha / Колір фону)', True, '', saved.get('autoCrop', False))
            auto_crop.isVisible = True

            if init_img_path and os.path.isfile(init_img_path):
                try:
                    from PIL import Image
                    with Image.open(init_img_path) as _init_im:
                        if _init_im.size[1] > 0:
                            _img_aspect_ratio = float(_init_im.size[0]) / float(_init_im.size[1])
                except:
                    pass

            # Співвідношення сторін та масштабування
            aspect_dd = inputs.addDropDownCommandInput('aspectRatio', 'Співвідношення сторін', adsk.core.DropDownStyles.TextListDropDownStyle)
            saved_aspect = saved.get('aspectRatio', 'За пропорціями картинки (Оригінал)')
            for a_name in ['За пропорціями картинки (Оригінал)', '1:1 (Квадрат / Коло)', '4:3 (Стандарт)', '16:9 (Широкий)', '3:2 (Фото)', 'Довільне (Вручну)']:
                aspect_dd.listItems.add(a_name, a_name == saved_aspect, '')

            fit_dd = inputs.addDropDownCommandInput('fitMode', 'Масштабування картинки', adsk.core.DropDownStyles.TextListDropDownStyle)
            saved_fit = saved.get('fitMode', 'Вписати зі збереженням пропорцій (Fit)')
            for f_name in ['Вписати зі збереженням пропорцій (Fit)', 'Заповнити (Fill / Обрізка)', 'Розтягнути по формі (Stretch)']:
                fit_dd.listItems.add(f_name, f_name == saved_fit, '')

            # Пресет Літофанії
            inputs.addBoolValueInput('presetLithophane', '⚡ Застосувати пресет «Літофанія» (для 3D-друку картинок)', False, '', False)

            # ---- Форма заготовки ----
            shape_dd = inputs.addDropDownCommandInput('shapeType', 'Форма заготовки', adsk.core.DropDownStyles.TextListDropDownStyle)
            saved_shape = saved.get('shapeType', 'Прямокутник')
            for s_name in ['Прямокутник', 'Коло', 'Овал', 'Багатокутник', 'Контур зображення (Alpha / Колір фону)']:
                is_sel = (s_name == saved_shape) or (saved_shape in ('Коло / Овал', 'Коло') and s_name == 'Коло') or (saved_shape.startswith('Контур зображення') and s_name.startswith('Контур зображення'))
                shape_dd.listItems.add(s_name, is_sel, '')

            mm = 'mm'
            def_w = f"{saved.get('rectWidth', 100.0)} mm"
            def_l = f"{saved.get('rectLength', 100.0)} mm"
            def_dia = f"{saved.get('circleDia', saved.get('rectWidth', 100.0))} mm"
            def_dx = f"{saved.get('circleDiaX', 100.0)} mm"
            def_dy = f"{saved.get('circleDiaY', 100.0)} mm"
            def_r = f"{saved.get('polyRadius', 50.0)} mm"

            is_contour_init = saved_shape.startswith('Контур зображення')
            is_rect_init = (saved_shape == 'Прямокутник')
            is_circle_init = (saved_shape in ('Коло', 'Коло / Овал'))
            is_oval_init = (saved_shape == 'Овал')
            is_poly_init = (saved_shape == 'Багатокутник')
            drop_bg_init = saved.get('dropBackground', True)

            rect_w = inputs.addValueInput('rectWidth', 'Ширина (X)', mm, adsk.core.ValueInput.createByString(def_w))
            rect_w.isVisible = (is_rect_init or is_contour_init)
            rect_l = inputs.addValueInput('rectLength', 'Довжина (Y)', mm, adsk.core.ValueInput.createByString(def_l))
            rect_l.isVisible = (is_rect_init or is_contour_init)
            aspect_dd.isVisible = (is_rect_init or is_contour_init or is_oval_init)

            circle_d = inputs.addValueInput('circleDia', 'Діаметр кола', mm, adsk.core.ValueInput.createByString(def_dia))
            circle_d.isVisible = is_circle_init

            circle_dx = inputs.addValueInput('circleDiaX', 'Діаметр X (Ширина)', mm, adsk.core.ValueInput.createByString(def_dx))
            circle_dx.isVisible = is_oval_init
            circle_dy = inputs.addValueInput('circleDiaY', 'Діаметр Y (Довжина)', mm, adsk.core.ValueInput.createByString(def_dy))
            circle_dy.isVisible = is_oval_init

            poly_sides = inputs.addIntegerSpinnerCommandInput('polySides', 'Кількість граней', 3, 36, 1, saved.get('polySides', 6))
            poly_sides.isVisible = is_poly_init
            poly_r = inputs.addValueInput('polyRadius', 'Радіус (до вершини)', mm, adsk.core.ValueInput.createByString(def_r))
            poly_r.isVisible = is_poly_init

            drop_bg = inputs.addBoolValueInput('dropBackground', 'Вирівнювати колір фону до Z = 0 (плоска основа)', True, '', drop_bg_init)
            drop_bg.isVisible = not is_contour_init

            alpha_thresh = inputs.addIntegerSpinnerCommandInput('alphaThreshold', 'Чутливість виявлення фону (1-254)', 1, 254, 1, saved.get('alphaThreshold', 32))
            alpha_thresh.isVisible = (is_contour_init or drop_bg_init)
            fill_holes = inputs.addBoolValueInput('fillHoles', 'Суцільна основа (запобігати діркам у тінях)', True, '', saved.get('fillHoles', True))
            fill_holes.isVisible = is_contour_init

            # ---- Орієнтація початку координат ----
            inputs.addSeparatorCommandInput('sep_origin')
            origin_dd = inputs.addDropDownCommandInput('originAlign', 'Початок координат (XYZ Origin)', adsk.core.DropDownStyles.TextListDropDownStyle)
            saved_origin = saved.get('originAlign', 'Лівий нижній кут у (0, 0, 0)')
            for o_name in ['Лівий нижній кут у (0, 0, 0)', 'Центр моделі у точці (0, 0, 0)', 'Верхня площина рельєфу Z = 0']:
                origin_dd.listItems.add(o_name, o_name == saved_origin, '')

            # ---- Параметри рельєфу ----
            inputs.addSeparatorCommandInput('sep1')
            inputs.addValueInput('maxDepth', 'Глибина рельєфу (макс. висота)', mm, adsk.core.ValueInput.createByString(f"{saved.get('maxDepth', 2.0)} mm"))
            inputs.addValueInput('baseThickness', 'Висота заготовки (товщина підкладки, >=0)', mm, adsk.core.ValueInput.createByString(f"{saved.get('baseThickness', 5.0)} mm"))
            inputs.addValueInput('vertexSpacing', 'Крок сітки (деталізація)', mm, adsk.core.ValueInput.createByString(f"{saved.get('vertexSpacing', 0.5)} mm"))
            inputs.addIntegerSpinnerCommandInput('smoothPasses', 'Згладжування рельєфу (0 = вимк, 1-10)', 0, 10, 1, saved.get('smoothPasses', 1))

            # Плавний згасаючий край (Vignette Fade)
            enable_fade = inputs.addBoolValueInput('enableVignetteFade', 'Плавне згасання країв до основи (Vignette Fade)', True, '', saved.get('enableVignetteFade', False))
            fade_w = inputs.addValueInput('fadeWidth', 'Ширина згасання країв (мм)', mm, adsk.core.ValueInput.createByString(f"{saved.get('fadeWidth', 5.0)} mm"))
            fade_w.isVisible = saved.get('enableVignetteFade', False)

            # Інверсія та гамма
            inputs.addBoolValueInput('invertHeight', 'Інвертувати висоту (чорний = випуклий)', True, '', saved.get('invertHeight', True))
            inputs.addValueInput('gammaVal', 'Гамма-корекція (1.0 = норма, <1 світліше, >1 контрастніше)', '', adsk.core.ValueInput.createByReal(saved.get('gammaVal', 1.0)))

            # ---- Тверда BRep основа (Solid Plate) та CAM-опції ----
            inputs.addSeparatorCommandInput('sep2')
            create_solid = saved.get('createSolidBase', False)
            inputs.addBoolValueInput('createSolidBase', 'Створювати окрему тверду основу (Solid BRep плиту)', True, '', create_solid)
            solid_thick = inputs.addValueInput('solidBaseThickness', 'Товщина твердотільної плити під рельєфом', mm, adsk.core.ValueInput.createByString(f"{saved.get('solidBaseThickness', 5.0)} mm"))
            solid_thick.isVisible = create_solid

            # Фаска / Скруглення основи
            edge_dd = inputs.addDropDownCommandInput('edgeTreatment', 'Обробка країв основи', adsk.core.DropDownStyles.TextListDropDownStyle)
            saved_edge = saved.get('edgeTreatment', 'Немає')
            for e_name in ['Немає', 'Скруглення (Fillet)', 'Фаска (Chamfer)']:
                edge_dd.listItems.add(e_name, e_name == saved_edge, '')
            edge_dd.isVisible = create_solid

            edge_r = inputs.addValueInput('edgeRadius', 'Розмір фаски / радіус (мм)', mm, adsk.core.ValueInput.createByString(f"{saved.get('edgeRadius', 2.0)} mm"))
            edge_r.isVisible = create_solid and (saved_edge != 'Немає')

            # Монтажні отвори
            create_holes = saved.get('createMountingHoles', False)
            holes_input = inputs.addBoolValueInput('createMountingHoles', 'Додати отвори під гвинти у кутах', True, '', create_holes)
            holes_input.isVisible = create_solid

            hole_dd = inputs.addDropDownCommandInput('holeType', 'Розмір гвинта', adsk.core.DropDownStyles.TextListDropDownStyle)
            saved_hole = saved.get('holeType', 'M4 (4.3 мм)')
            for h_name in ['M3 (3.2 мм)', 'M4 (4.3 мм)', 'M5 (5.3 мм)', 'M6 (6.4 мм)']:
                hole_dd.listItems.add(h_name, h_name == saved_hole, '')
            hole_dd.isVisible = create_solid and create_holes

            hole_off = inputs.addValueInput('holeOffset', 'Відступ отворів від кутів (мм)', mm, adsk.core.ValueInput.createByString(f"{saved.get('holeOffset', 8.0)} mm"))
            hole_off.isVisible = create_solid and create_holes

            on_validate = CommandValidateInputsHandler()
            cmd.validateInputs.add(on_validate)
            handlers.append(on_validate)

            on_execute = CommandExecuteHandler()
            cmd.execute.add(on_execute)
            handlers.append(on_execute)

            on_input_changed = InputChangedHandler()
            cmd.inputChanged.add(on_input_changed)
            handlers.append(on_input_changed)

            on_destroy = CommandDestroyHandler()
            cmd.destroy.add(on_destroy)
            handlers.append(on_destroy)
        except:
            if _ui:
                _ui.messageBox('Помилка створення діалогу:\n{}'.format(traceback.format_exc()))


class CommandValidateInputsHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            inputs = args.inputs
            img_input = inputs.itemById('imagePath')
            if not img_input or not img_input.value or not os.path.isfile(img_input.value):
                args.areInputsValid = False
                return

            gamma_input = inputs.itemById('gammaVal')
            if gamma_input and gamma_input.value <= 0:
                args.areInputsValid = False
                return

            max_d = inputs.itemById('maxDepth')
            base_t = inputs.itemById('baseThickness')
            if not max_d or not base_t or max_d.value < 0 or base_t.value < 0:
                args.areInputsValid = False
                return

            shape_dd = inputs.itemById('shapeType')
            if shape_dd and shape_dd.selectedItem:
                shape = shape_dd.selectedItem.name
                if shape == 'Прямокутник' or shape.startswith('Контур зображення'):
                    w = inputs.itemById('rectWidth')
                    l = inputs.itemById('rectLength')
                    if not w or not l or w.value <= 0 or l.value <= 0:
                        args.areInputsValid = False
                        return
                elif shape == 'Коло':
                    d = inputs.itemById('circleDia')
                    if not d or d.value <= 0:
                        args.areInputsValid = False
                        return
                elif shape in ('Овал', 'Коло / Овал'):
                    dx = inputs.itemById('circleDiaX')
                    dy = inputs.itemById('circleDiaY')
                    if not dx or not dy or dx.value <= 0 or dy.value <= 0:
                        args.areInputsValid = False
                        return

            create_solid_input = inputs.itemById('createSolidBase')
            if create_solid_input and create_solid_input.value:
                thick_input = inputs.itemById('solidBaseThickness')
                if not thick_input or thick_input.value <= 0:
                    args.areInputsValid = False
                    return

            args.areInputsValid = True
        except:
            args.areInputsValid = False


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        global _is_updating_aspect, _img_aspect_ratio
        try:
            changed = args.input
            inputs = args.inputs

            # Пресет «Літофанія»
            if changed.id == 'presetLithophane':
                bool_val = adsk.core.BoolValueCommandInput.cast(changed).value
                if bool_val:
                    inputs.itemById('invertHeight').value = True
                    inputs.itemById('maxDepth').value = 0.24  # 2.4 мм
                    inputs.itemById('baseThickness').value = 0.08  # 0.8 мм
                    inputs.itemById('fillHoles').value = True
                    _ui.messageBox('Застосовано налаштування для Літофанії:\n- Інверсія: Так\n- Глибина: 2.4 мм\n- Товщина основи: 0.8 мм')

            # Перемикання згасання країв
            if changed.id == 'enableVignetteFade':
                is_fade = adsk.core.BoolValueCommandInput.cast(changed).value
                inputs.itemById('fadeWidth').isVisible = is_fade

            # Вибір зображення
            if changed.id == 'browseBtn':
                bool_input = adsk.core.BoolValueCommandInput.cast(changed)
                if bool_input.value:
                    file_dlg = _ui.createFileDialog()
                    file_dlg.title = 'Обрати карту висот'
                    file_dlg.filter = 'Зображення (*.png;*.tif;*.tiff;*.jpg;*.jpeg;*.bmp)'
                    file_dlg.isMultiSelectEnabled = False
                    result = file_dlg.showOpen()
                    if result == adsk.core.DialogResults.DialogOK:
                        path_input = adsk.core.StringValueCommandInput.cast(inputs.itemById('imagePath'))
                        path_input.value = file_dlg.filename

                        try:
                            from PIL import Image
                            with Image.open(file_dlg.filename) as probe_img:
                                shape_dd = inputs.itemById('shapeType')
                                shape = shape_dd.selectedItem.name if shape_dd else 'Прямокутник'
                                auto_crop_input = inputs.itemById('autoCrop')
                                auto_crop = (auto_crop_input.value if auto_crop_input else False) and shape.startswith('Контур зображення')

                                alpha_thresh_input = inputs.itemById('alphaThreshold')
                                thresh = alpha_thresh_input.value if alpha_thresh_input else 32

                                bbox = get_content_bbox(probe_img, only_alpha=False, threshold=thresh) if auto_crop else None
                                if bbox:
                                    pw = bbox[2] - bbox[0]
                                    ph = bbox[3] - bbox[1]
                                else:
                                    pw, ph = probe_img.size

                                if ph > 0:
                                    _img_aspect_ratio = float(pw) / float(ph)

                                    aspect_input = inputs.itemById('aspectRatio')
                                    aspect_name = aspect_input.selectedItem.name if aspect_input and aspect_input.selectedItem else 'За пропорціями картинки (Оригінал)'
                                    if aspect_name == 'За пропорціями картинки (Оригінал)':
                                        _is_updating_aspect = True
                                        rw = adsk.core.ValueCommandInput.cast(inputs.itemById('rectWidth'))
                                        rl = adsk.core.ValueCommandInput.cast(inputs.itemById('rectLength'))
                                        if rw and rl:
                                            rl.value = rw.value / _img_aspect_ratio
                                        _is_updating_aspect = False
                        except:
                            pass
                    bool_input.value = False

            # Перемикання форми
            if changed.id == 'shapeType':
                dd = adsk.core.DropDownCommandInput.cast(changed)
                shape = dd.selectedItem.name
                is_rect = (shape == 'Прямокутник')
                is_circle = (shape == 'Коло')
                is_oval = (shape in ('Овал', 'Коло / Овал'))
                is_poly = (shape == 'Багатокутник')
                is_alpha = shape.startswith('Контур зображення')

                inputs.itemById('rectWidth').isVisible = (is_rect or is_alpha)
                inputs.itemById('rectLength').isVisible = (is_rect or is_alpha)
                if inputs.itemById('aspectRatio'):
                    inputs.itemById('aspectRatio').isVisible = (is_rect or is_alpha or is_oval)

                if inputs.itemById('circleDia'):
                    inputs.itemById('circleDia').isVisible = is_circle
                inputs.itemById('circleDiaX').isVisible = is_oval
                inputs.itemById('circleDiaY').isVisible = is_oval
                inputs.itemById('polySides').isVisible = is_poly
                inputs.itemById('polyRadius').isVisible = is_poly

                drop_bg_input = inputs.itemById('dropBackground')
                if drop_bg_input:
                    drop_bg_input.isVisible = not is_alpha
                drop_bg_val = drop_bg_input.value if drop_bg_input else True

                inputs.itemById('alphaThreshold').isVisible = (is_alpha or drop_bg_val)
                inputs.itemById('fillHoles').isVisible = is_alpha
                inputs.itemById('autoCrop').isVisible = True

            if changed.id == 'dropBackground':
                shape_item = inputs.itemById('shapeType').selectedItem if inputs.itemById('shapeType') else None
                shape = shape_item.name if shape_item else ''
                is_alpha = shape.startswith('Контур зображення')
                if not is_alpha and inputs.itemById('alphaThreshold'):
                    inputs.itemById('alphaThreshold').isVisible = changed.value

            # Перемикання співвідношення сторін або зміна розмірів
            if changed.id == 'aspectRatio':
                dd = adsk.core.DropDownCommandInput.cast(changed)
                aspect_name = dd.selectedItem.name if dd.selectedItem else ''
                ratio = get_aspect_ratio_value(aspect_name, _img_aspect_ratio)
                if ratio is not None and ratio > 0:
                    _is_updating_aspect = True
                    rw = adsk.core.ValueCommandInput.cast(inputs.itemById('rectWidth'))
                    rl = adsk.core.ValueCommandInput.cast(inputs.itemById('rectLength'))
                    if rw and rl:
                        rl.value = rw.value / ratio
                    dx = adsk.core.ValueCommandInput.cast(inputs.itemById('circleDiaX'))
                    dy = adsk.core.ValueCommandInput.cast(inputs.itemById('circleDiaY'))
                    if dx and dy:
                        dy.value = dx.value / ratio
                    _is_updating_aspect = False

            if not _is_updating_aspect:
                aspect_input = inputs.itemById('aspectRatio')
                aspect_name = aspect_input.selectedItem.name if aspect_input and aspect_input.selectedItem else 'Довільне (Вручну)'
                ratio = get_aspect_ratio_value(aspect_name, _img_aspect_ratio)
                if ratio is not None and ratio > 0:
                    if changed.id == 'rectWidth':
                        _is_updating_aspect = True
                        rw = adsk.core.ValueCommandInput.cast(changed)
                        rl = adsk.core.ValueCommandInput.cast(inputs.itemById('rectLength'))
                        if rw and rl:
                            rl.value = rw.value / ratio
                        _is_updating_aspect = False
                    elif changed.id == 'rectLength':
                        _is_updating_aspect = True
                        rl = adsk.core.ValueCommandInput.cast(changed)
                        rw = adsk.core.ValueCommandInput.cast(inputs.itemById('rectWidth'))
                        if rw and rl:
                            rw.value = rl.value * ratio
                        _is_updating_aspect = False
                    elif changed.id == 'circleDiaX':
                        _is_updating_aspect = True
                        dx = adsk.core.ValueCommandInput.cast(changed)
                        dy = adsk.core.ValueCommandInput.cast(inputs.itemById('circleDiaY'))
                        if dx and dy:
                            dy.value = dx.value / ratio
                        _is_updating_aspect = False
                    elif changed.id == 'circleDiaY':
                        _is_updating_aspect = True
                        dy = adsk.core.ValueCommandInput.cast(changed)
                        dx = adsk.core.ValueCommandInput.cast(inputs.itemById('circleDiaX'))
                        if dx and dy:
                            dx.value = dy.value * ratio
                        _is_updating_aspect = False

            # Перемикання твердотільної основи та CAM інструментів
            if changed.id == 'createSolidBase' or changed.id == 'createMountingHoles' or changed.id == 'edgeTreatment':
                is_solid = adsk.core.BoolValueCommandInput.cast(inputs.itemById('createSolidBase')).value
                inputs.itemById('solidBaseThickness').isVisible = is_solid
                inputs.itemById('edgeTreatment').isVisible = is_solid

                edge_val = inputs.itemById('edgeTreatment').selectedItem.name if inputs.itemById('edgeTreatment') and inputs.itemById('edgeTreatment').selectedItem else 'Немає'
                inputs.itemById('edgeRadius').isVisible = is_solid and (edge_val != 'Немає')

                inputs.itemById('createMountingHoles').isVisible = is_solid
                is_holes = inputs.itemById('createMountingHoles').value if inputs.itemById('createMountingHoles') else False
                inputs.itemById('holeType').isVisible = is_solid and is_holes
                inputs.itemById('holeOffset').isVisible = is_solid and is_holes

            # Оновлення інформаційної панелі
            def_mm = lambda i_id: adsk.core.ValueCommandInput.cast(inputs.itemById(i_id)).value * 10.0 if inputs.itemById(i_id) else 100.0
            shape_val = inputs.itemById('shapeType').selectedItem.name if inputs.itemById('shapeType') and inputs.itemById('shapeType').selectedItem else 'Прямокутник'
            if shape_val == 'Коло':
                w_mm = l_mm = def_mm('circleDia')
            elif shape_val in ('Овал', 'Коло / Овал'):
                w_mm = def_mm('circleDiaX')
                l_mm = def_mm('circleDiaY')
            elif shape_val == 'Багатокутник':
                w_mm = l_mm = def_mm('polyRadius') * 2.0
            else:
                w_mm = def_mm('rectWidth')
                l_mm = def_mm('rectLength')
            sp_mm = def_mm('vertexSpacing')
            if sp_mm > 0 and w_mm > 0 and l_mm > 0:
                cols = max(2, int(round(w_mm / sp_mm)) + 1)
                rows = max(2, int(round(l_mm / sp_mm)) + 1)
                verts_cnt = cols * rows
                tris_cnt = (cols - 1) * (rows - 1) * 2
                backend_str = "NumPy ⚡ (30 мс)" if HAS_NUMPY else "Python (~1.5 сек)"
                info_box = inputs.itemById('infoBox')
                if info_box:
                    info_box.formattedText = f"<b>Сітка:</b> {cols}×{rows} | <b>Вершин:</b> {verts_cnt:,} | <b>Трикутників:</b> {tris_cnt:,}<br><b>Двигун:</b> {backend_str}"

        except:
            if _ui:
                _ui.messageBox('Помилка:\n{}'.format(traceback.format_exc()))


class CommandDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        adsk.terminate()


# =====================================================================
# Виконання (натиснуто OK)
# =====================================================================

class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        progress = None
        try:
            inputs = args.command.commandInputs

            image_path = adsk.core.StringValueCommandInput.cast(inputs.itemById('imagePath')).value
            if not image_path or not os.path.isfile(image_path):
                _ui.messageBox('Файл зображення не обрано або не знайдено.')
                return

            if not ensure_pillow(_ui):
                return
            from PIL import Image, ImageFilter

            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                _ui.messageBox('Немає активного Fusion-документа.')
                return
            if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
                _ui.messageBox('Вставка мешу вимагає параметричного режиму.\n\nУвімкни "Capture design history" і запусти команду ще раз.')
                return

            def val_mm(input_id):
                return adsk.core.ValueCommandInput.cast(inputs.itemById(input_id)).value * 10.0

            def int_val(input_id):
                return adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById(input_id)).value

            shape = adsk.core.DropDownCommandInput.cast(inputs.itemById('shapeType')).selectedItem.name
            origin_mode = adsk.core.DropDownCommandInput.cast(inputs.itemById('originAlign')).selectedItem.name

            max_depth = val_mm('maxDepth')
            base_thickness = val_mm('baseThickness')
            vertex_spacing = val_mm('vertexSpacing')
            if vertex_spacing <= 0: vertex_spacing = 0.5
            smooth_passes = int_val('smoothPasses')

            enable_vignette = inputs.itemById('enableVignetteFade').value if inputs.itemById('enableVignetteFade') else False
            fade_width = val_mm('fadeWidth') if enable_vignette else 0.0

            invert_height = adsk.core.BoolValueCommandInput.cast(inputs.itemById('invertHeight')).value
            gamma_val = adsk.core.ValueCommandInput.cast(inputs.itemById('gammaVal')).value if inputs.itemById('gammaVal') else 1.0
            if gamma_val <= 0: gamma_val = 1.0

            auto_crop_input = inputs.itemById('autoCrop')
            auto_crop = auto_crop_input.value if auto_crop_input else False

            create_solid_base = inputs.itemById('createSolidBase').value if inputs.itemById('createSolidBase') else False
            solid_base_thickness = val_mm('solidBaseThickness') if create_solid_base else 0.0

            edge_treatment = inputs.itemById('edgeTreatment').selectedItem.name if inputs.itemById('edgeTreatment') and inputs.itemById('edgeTreatment').selectedItem else 'Немає'
            edge_radius = val_mm('edgeRadius') if create_solid_base else 0.0

            create_holes = inputs.itemById('createMountingHoles').value if inputs.itemById('createMountingHoles') and create_solid_base else False
            hole_type = inputs.itemById('holeType').selectedItem.name if inputs.itemById('holeType') and inputs.itemById('holeType').selectedItem else 'M4 (4.3 мм)'
            hole_offset = val_mm('holeOffset') if create_holes else 0.0

            hole_dia_map = {'M3 (3.2 мм)': 3.2, 'M4 (4.3 мм)': 4.3, 'M5 (5.3 мм)': 5.3, 'M6 (6.4 мм)': 6.4}
            hole_dia_mm = hole_dia_map.get(hole_type, 4.3)

            fit_mode_input = inputs.itemById('fitMode')
            fit_mode = fit_mode_input.selectedItem.name if fit_mode_input and fit_mode_input.selectedItem else 'Вписати зі збереженням пропорцій (Fit)'

            # Збереження налаштувань
            save_last_params({
                'imagePath': image_path,
                'shapeType': shape,
                'originAlign': origin_mode,
                'rectWidth': val_mm('rectWidth') if inputs.itemById('rectWidth') else 100.0,
                'rectLength': val_mm('rectLength') if inputs.itemById('rectLength') else 100.0,
                'circleDia': val_mm('circleDia') if inputs.itemById('circleDia') else 100.0,
                'circleDiaX': val_mm('circleDiaX') if inputs.itemById('circleDiaX') else 100.0,
                'circleDiaY': val_mm('circleDiaY') if inputs.itemById('circleDiaY') else 100.0,
                'polySides': int_val('polySides') if inputs.itemById('polySides') else 6,
                'polyRadius': val_mm('polyRadius') if inputs.itemById('polyRadius') else 50.0,
                'alphaThreshold': int_val('alphaThreshold') if inputs.itemById('alphaThreshold') else 32,
                'fillHoles': inputs.itemById('fillHoles').value if inputs.itemById('fillHoles') else True,
                'dropBackground': inputs.itemById('dropBackground').value if inputs.itemById('dropBackground') else True,
                'autoCrop': inputs.itemById('autoCrop').value if inputs.itemById('autoCrop') else False,
                'aspectRatio': inputs.itemById('aspectRatio').selectedItem.name if inputs.itemById('aspectRatio') and inputs.itemById('aspectRatio').selectedItem else 'За пропорціями картинки (Оригінал)',
                'fitMode': fit_mode,
                'maxDepth': max_depth,
                'baseThickness': base_thickness,
                'vertexSpacing': vertex_spacing,
                'smoothPasses': smooth_passes,
                'enableVignetteFade': enable_vignette,
                'fadeWidth': fade_width,
                'invertHeight': invert_height,
                'gammaVal': gamma_val,
                'createSolidBase': create_solid_base,
                'solidBaseThickness': solid_base_thickness,
                'edgeTreatment': edge_treatment,
                'edgeRadius': edge_radius,
                'createMountingHoles': create_holes,
                'holeType': hole_type,
                'holeOffset': hole_offset
            })

            # Габарити
            if shape == 'Прямокутник' or shape.startswith('Контур зображення'):
                width_mm = val_mm('rectWidth')
                height_mm = val_mm('rectLength')
            elif shape == 'Коло':
                width_mm = height_mm = val_mm('circleDia')
            elif shape in ('Овал', 'Коло / Овал'):
                width_mm = val_mm('circleDiaX')
                height_mm = val_mm('circleDiaY')
            elif shape == 'Багатокутник':
                radius = val_mm('polyRadius')
                width_mm = height_mm = radius * 2.0

            # Зміщення нуля координат (XYZ Origin Shift)
            if origin_mode == 'Центр моделі у точці (0, 0, 0)':
                shift_x, shift_y = -width_mm / 2.0, -height_mm / 2.0
                shift_z = 0.0
            elif origin_mode == 'Верхня площина рельєфу Z = 0':
                shift_x, shift_y = 0.0, 0.0
                shift_z = -(base_thickness + max_depth)
            else:  # Лівий нижній кут у (0, 0, 0)
                shift_x, shift_y, shift_z = 0.0, 0.0, 0.0

            grid_cols = max(2, int(round(width_mm / vertex_spacing)) + 1)
            grid_rows = max(2, int(round(height_mm / vertex_spacing)) + 1)

            progress = _ui.createProgressDialog()
            progress.isCancelButtonShown = True
            progress.show("Генерація 3D-рельєфу", "Завантаження зображення...", 0, 100, 1)

            resample_filter = getattr(Image, 'Resampling', Image).BILINEAR
            img_raw = Image.open(image_path)

            if auto_crop:
                crop_thresh = int_val('alphaThreshold') if inputs.itemById('alphaThreshold') else 32
                bbox = get_content_bbox(img_raw, only_alpha=False, threshold=crop_thresh)
                if bbox: img_raw = img_raw.crop(bbox)

            has_alpha = (img_raw.mode in ('RGBA', 'LA') or ('transparency' in img_raw.info))
            if has_alpha:
                img_rgba = img_raw.convert('RGBA')
                img_resized_alpha = fit_image_to_grid(img_rgba, grid_cols, grid_rows, fit_mode=fit_mode)
                alpha_bytes = img_resized_alpha.split()[-1].tobytes()
            else:
                alpha_bytes = None

            is_16bit = img_raw.mode in ('I;16', 'I;16L', 'I;16B', 'I', 'F') or getattr(img_raw, 'bits', 8) == 16

            progress.progressValue = 25
            progress.message = f"Фільтрація та згладжування ({'NumPy' if HAS_NUMPY else 'Python'})..."
            if progress.wasCancelled: return

            if is_16bit:
                img_conv = img_raw.convert('I')
                img_resized = fit_image_to_grid(img_conv, grid_cols, grid_rows, fit_mode=fit_mode)
                max_val = 65535.0
                lut_size = 65536
                img_resized_rgb = None
                rgb_arr = None
                rgb_bytes = None
            else:
                if has_alpha:
                    img_gray = img_resized_alpha.convert('L')
                    img_resized_rgb = img_resized_alpha.convert('RGB')
                else:
                    img_rgb = img_raw.convert('RGB')
                    img_resized_rgb = fit_image_to_grid(img_rgb, grid_cols, grid_rows, fit_mode=fit_mode)
                    img_gray = img_resized_rgb.convert('L')
                max_val = 255.0
                lut_size = 256
                rgb_arr = np.asarray(img_resized_rgb, dtype=np.uint8) if HAS_NUMPY else None
                rgb_bytes = img_resized_rgb.tobytes() if not HAS_NUMPY else None

            # Підготовка масивів пікселів для поточної сітки
            if HAS_NUMPY:
                raw_arr = np.clip(np.asarray(img_resized, dtype=np.int32), 0, 65535) if is_16bit else np.asarray(img_gray, dtype=np.uint8)
                raw_pixels = None
            else:
                raw_arr = None
                raw_pixels = (array.array('I', img_resized.tobytes()) if is_16bit else img_gray.tobytes())

            # LUT
            lut = [0.0] * lut_size
            for i in range(lut_size):
                v = i / max_val
                if invert_height: v = 1.0 - v
                if gamma_val != 1.0 and gamma_val > 0: v = math.pow(max(0.0, min(1.0, v)), gamma_val)
                lut[i] = v * max_depth

            progress.progressValue = 45
            progress.message = "Розрахунок 3D-вершин та Vignette Fade..."
            if progress.wasCancelled: return

            step_x = width_mm / (grid_cols - 1)
            step_y = height_mm / (grid_rows - 1)

            is_contour = shape.startswith('Контур зображення')
            drop_bg = inputs.itemById('dropBackground').value if inputs.itemById('dropBackground') else True
            threshold = int_val('alphaThreshold') if inputs.itemById('alphaThreshold') else 32
            fill_holes = inputs.itemById('fillHoles').value if inputs.itemById('fillHoles') else True

            # Виявлення маски фону
            bg_object_mask = None
            mask_bytes = None
            if is_contour or drop_bg:
                mask_bytes = detect_outer_mask(
                    grid_cols, grid_rows,
                    alpha_bytes=alpha_bytes,
                    rgb_arr=rgb_arr,
                    rgb_bytes=rgb_bytes,
                    raw_pixels=raw_pixels,
                    raw_arr=raw_arr,
                    is_16bit=is_16bit,
                    threshold=threshold,
                    fill_holes=fill_holes
                )
                if HAS_NUMPY:
                    bg_object_mask = np.array(mask_bytes, dtype=bool).reshape((grid_rows, grid_cols))

            if HAS_NUMPY:
                lut_arr = np.array(lut, dtype=np.float32)
                z_rel = np.take(lut_arr, raw_arr)

                # Опускаємо колір фону до Z=0
                if bg_object_mask is not None and (drop_bg or is_contour):
                    z_rel[~bg_object_mask] = 0.0

                # Реальне плаваюче 2D-гаусове згладжування висот
                if smooth_passes > 0:
                    sigma_val = float(smooth_passes) * 0.9 + 0.3
                    z_rel = gaussian_blur_2d(z_rel, sigma=sigma_val)
                    if bg_object_mask is not None and drop_bg:
                        z_rel[~bg_object_mask] = 0.0

                y_idx, x_idx = np.ogrid[:grid_rows, :grid_cols]
                x_grid = x_idx * step_x
                y_grid = (grid_rows - 1 - y_idx) * step_y

                # Vignette Fade
                if enable_vignette and fade_width > 0:
                    d_left = x_grid
                    d_right = width_mm - x_grid
                    d_bottom = y_grid
                    d_top = height_mm - y_grid
                    d_min = np.minimum(np.minimum(d_left, d_right), np.minimum(d_bottom, d_top))
                    t_fade = np.clip(d_min / fade_width, 0.0, 1.0)
                    smooth_f = 3.0 * (t_fade ** 2) - 2.0 * (t_fade ** 3)
                    z_rel = z_rel * smooth_f

                z_grid = z_rel + base_thickness + shift_z
                x_grid_shifted = x_grid + shift_x
                y_grid_shifted = y_grid + shift_y

                all_vertices = np.column_stack((
                    np.tile(x_grid_shifted, (grid_rows, 1)).ravel(),
                    np.tile(y_grid_shifted, (1, grid_cols)).ravel(),
                    z_grid.ravel()
                ))
                all_vertices_list = [tuple(v) for v in all_vertices]

                progress.progressValue = 65
                progress.message = "Побудова трикутної сітки та стінок (NumPy)..."
                if progress.wasCancelled: return

                if shape == 'Прямокутник':
                    inside_mask = np.ones((grid_rows, grid_cols), dtype=bool)
                elif shape == 'Коло':
                    cx, cy = width_mm / 2.0, height_mm / 2.0
                    r = width_mm / 2.0
                    inside_mask = (((x_grid - cx) ** 2 + (y_grid - cy) ** 2) <= (r ** 2 + 1e-6))
                elif shape in ('Овал', 'Коло / Овал'):
                    cx, cy = width_mm / 2.0, height_mm / 2.0
                    rx, ry = width_mm / 2.0, height_mm / 2.0
                    inside_mask = (((x_grid - cx) / rx) ** 2 + ((y_grid - cy) / ry) ** 2) <= (1.0 + 1e-6)
                elif shape == 'Багатокутник':
                    sides = int_val('polySides')
                    radius = val_mm('polyRadius')
                    poly = make_regular_polygon(radius, radius, sides, radius)
                    mask_flat = bytearray(grid_cols * grid_rows)
                    idx = 0
                    for vy in [(grid_rows - 1 - y) * step_y for y in range(grid_rows)]:
                        for vx in [(x * step_x) for x in range(grid_cols)]:
                            if point_in_polygon(vx, vy, poly): mask_flat[idx] = 1
                            idx += 1
                    inside_mask = np.array(mask_flat, dtype=bool).reshape((grid_rows, grid_cols))
                else:
                    if bg_object_mask is not None:
                        inside_mask = bg_object_mask
                    else:
                        mask_bytes = detect_outer_mask(grid_cols, grid_rows, alpha_bytes=alpha_bytes, rgb_arr=rgb_arr, rgb_bytes=rgb_bytes, raw_pixels=raw_pixels, raw_arr=raw_arr, is_16bit=is_16bit, threshold=threshold, fill_holes=fill_holes)
                        inside_mask = np.array(mask_bytes, dtype=bool).reshape((grid_rows, grid_cols))

                v00 = (np.arange(grid_rows - 1)[:, None] * grid_cols + np.arange(grid_cols - 1)[None, :]).ravel()
                v10 = v00 + 1; v01 = v00 + grid_cols; v11 = v01 + 1

                if shape == 'Прямокутник':
                    tri1 = np.column_stack((v00, v01, v11))
                    tri2 = np.column_stack((v00, v11, v10))
                    top_triangles = np.vstack((tri1, tri2)).tolist()
                    boundary_edges = []
                    for y in range(grid_rows - 1):
                        row0 = y * grid_cols; row1 = (y + 1) * grid_cols
                        for x in range(grid_cols - 1):
                            if y == 0: boundary_edges.append((row0 + x + 1, row0 + x))
                            if y == grid_rows - 2: boundary_edges.append((row1 + x, row1 + x + 1))
                            if x == 0: boundary_edges.append((row0 + x, row1 + x))
                            if x == grid_cols - 2: boundary_edges.append((row1 + x + 1, row0 + x + 1))
                    vertices = all_vertices_list
                else:
                    m00 = inside_mask[:-1, :-1].ravel(); m10 = inside_mask[:-1, 1:].ravel()
                    m01 = inside_mask[1:, :-1].ravel(); m11 = inside_mask[1:, 1:].ravel()
                    t1_mask = m00 & m01 & m11; t2_mask = m00 & m11 & m10
                    t1 = np.column_stack((v00[t1_mask], v01[t1_mask], v11[t1_mask]))
                    t2 = np.column_stack((v00[t2_mask], v11[t2_mask], v10[t2_mask]))
                    raw_triangles = np.vstack((t1, t2))
                    if len(raw_triangles) == 0:
                        _ui.messageBox('Обрана форма не охопила жодного трикутника.')
                        return
                    unique_v, remap = np.unique(raw_triangles, return_inverse=True)
                    top_triangles = remap.reshape(raw_triangles.shape).tolist()
                    vertices = [all_vertices_list[v] for v in unique_v]
                    boundary_edges_set = set()
                    for a, b, c in top_triangles:
                        for u, v in ((a, b), (b, c), (c, a)):
                            if (v, u) in boundary_edges_set: boundary_edges_set.remove((v, u))
                            else: boundary_edges_set.add((u, v))
                    boundary_edges = list(boundary_edges_set)

            else:
                raw_pixels = (array.array('I', img_resized.tobytes()) if is_16bit else img_gray.tobytes())
                vx_table = [x * step_x for x in range(grid_cols)]
                vy_table = [(grid_rows - 1 - y) * step_y for y in range(grid_rows)]

                all_z = [lut[min(65535, max(0, raw_pixels[i])) if is_16bit else raw_pixels[i]] for i in range(grid_cols * grid_rows)]

                if mask_bytes is not None and (drop_bg or is_contour):
                    for i in range(grid_cols * grid_rows):
                        if not mask_bytes[i]:
                            all_z[i] = 0.0

                if smooth_passes > 0:
                    all_z = smooth_grid_python(all_z, grid_cols, grid_rows, passes=smooth_passes)
                    if mask_bytes is not None and drop_bg:
                        for i in range(grid_cols * grid_rows):
                            if not mask_bytes[i]:
                                all_z[i] = 0.0

                all_vertices = [None] * (grid_cols * grid_rows)
                idx = 0
                for vy in vy_table:
                    adsk.doEvents()
                    d_y_edge = min(vy, height_mm - vy)
                    for vx in vx_table:
                        z_rel = all_z[idx]
                        if enable_vignette and fade_width > 0:
                            d_min = min(vx, width_mm - vx, d_y_edge)
                            t_fade = max(0.0, min(1.0, d_min / fade_width))
                            z_rel *= (3.0 * (t_fade ** 2) - 2.0 * (t_fade ** 3))
                        all_vertices[idx] = (vx + shift_x, vy + shift_y, z_rel + base_thickness + shift_z)
                        idx += 1

                progress.progressValue = 65
                progress.message = "Побудова трикутної сітки та стінок (Python)..."
                if progress.wasCancelled: return

                if shape == 'Прямокутник':
                    top_triangles = []
                    boundary_edges = []
                    for y in range(grid_rows - 1):
                        row0 = y * grid_cols; row1 = (y + 1) * grid_cols
                        for x in range(grid_cols - 1):
                            v00 = row0 + x; v10 = row0 + x + 1; v01 = row1 + x; v11 = row1 + x + 1
                            top_triangles.append((v00, v01, v11))
                            top_triangles.append((v00, v11, v10))
                            if y == 0: boundary_edges.append((v10, v00))
                            if y == grid_rows - 2: boundary_edges.append((v01, v11))
                            if x == 0: boundary_edges.append((v00, v01))
                            if x == grid_cols - 2: boundary_edges.append((v11, v10))
                    vertices = all_vertices
                else:
                    inside_mask = bytearray(grid_cols * grid_rows)
                    if shape == 'Коло':
                        cx, cy = width_mm / 2.0, height_mm / 2.0
                        r2 = (width_mm / 2.0) ** 2 + 1e-6
                        idx = 0
                        for vy in vy_table:
                            dy2 = (vy - cy) ** 2
                            for vx in vx_table:
                                if (vx - cx) ** 2 + dy2 <= r2: inside_mask[idx] = 1
                                idx += 1
                    elif shape in ('Овал', 'Коло / Овал'):
                        cx, cy = width_mm / 2.0, height_mm / 2.0
                        rx, ry = width_mm / 2.0, height_mm / 2.0
                        idx = 0
                        for vy in vy_table:
                            dy_norm = ((vy - cy) / ry) ** 2
                            for vx in vx_table:
                                if ((vx - cx) / rx) ** 2 + dy_norm <= 1.0 + 1e-6: inside_mask[idx] = 1
                                idx += 1
                    elif shape == 'Багатокутник':
                        sides = int_val('polySides')
                        radius = val_mm('polyRadius')
                        poly = make_regular_polygon(radius, radius, sides, radius)
                        idx = 0
                        for vy in vy_table:
                            for vx in vx_table:
                                if point_in_polygon(vx, vy, poly): inside_mask[idx] = 1
                                idx += 1
                    else:
                        inside_mask = mask_bytes if mask_bytes is not None else detect_outer_mask(grid_cols, grid_rows, alpha_bytes=alpha_bytes, rgb_arr=None, rgb_bytes=rgb_bytes, raw_pixels=raw_pixels, raw_arr=None, is_16bit=is_16bit, threshold=threshold, fill_holes=fill_holes)

                    top_triangles = []
                    append_tri = top_triangles.append
                    for y in range(grid_rows - 1):
                        row0 = y * grid_cols; row1 = (y + 1) * grid_cols
                        for x in range(grid_cols - 1):
                            if inside_mask[row0 + x] and inside_mask[row1 + x] and inside_mask[row1 + x + 1]: append_tri((row0 + x, row1 + x, row1 + x + 1))
                            if inside_mask[row0 + x] and inside_mask[row1 + x + 1] and inside_mask[row0 + x + 1]: append_tri((row0 + x, row1 + x + 1, row0 + x + 1))

                    if not top_triangles:
                        _ui.messageBox('Обрана форма не охопила жодного трикутника.')
                        return

                    remap = [-1] * (grid_cols * grid_rows)
                    vertices = []
                    for tri in top_triangles:
                        for v in tri:
                            if remap[v] == -1:
                                remap[v] = len(vertices)
                                vertices.append(all_vertices[v])

                    top_triangles = [(remap[a], remap[b], remap[c]) for a, b, c in top_triangles]
                    boundary_edges_set = set()
                    for a, b, c in top_triangles:
                        for u, v in ((a, b), (b, c), (c, a)):
                            if (v, u) in boundary_edges_set: boundary_edges_set.remove((v, u))
                            else: boundary_edges_set.add((u, v))
                    boundary_edges = list(boundary_edges_set)

            # Підкладка
            if base_thickness > 0:
                offset = len(vertices)
                bottom_vertices = [(vx, vy, shift_z) for (vx, vy, _) in vertices]
                vertices = vertices + bottom_vertices
                bottom_triangles = [(a + offset, c + offset, b + offset) for (a, b, c) in top_triangles]
                wall_triangles = []
                for (a, b) in boundary_edges:
                    wall_triangles.append((a, a + offset, b + offset))
                    wall_triangles.append((a, b + offset, b))
                triangles = top_triangles + bottom_triangles + wall_triangles
            else:
                triangles = top_triangles

            # Експорт у швидкий Binary STL
            progress.progressValue = 80
            progress.message = "Експорт у Binary STL..."
            if progress.wasCancelled: return

            stl_fp = tempfile.NamedTemporaryFile(mode='wb', suffix='.stl', delete=False)
            stl_fp.close()
            write_stl_binary(stl_fp.name, vertices, triangles, progress=progress)
            if progress.wasCancelled: return

            # Вставка MeshBody
            progress.progressValue = 88
            progress.message = "Вставка MeshBody у Fusion 360..."
            if progress.wasCancelled:
                try: os.remove(stl_fp.name)
                except OSError: pass
                return

            root_comp = design.rootComponent
            base_feat = root_comp.features.baseFeatures.add()
            base_feat.name = "Heightmap Base"
            base_feat.startEdit()
            try:
                _app.activeViewport.isUpdateLocked = True
                mesh_list = root_comp.meshBodies.add(stl_fp.name, adsk.fusion.MeshUnits.MillimeterMeshUnit, base_feat)
                if mesh_list.count > 0:
                    mesh_list.item(0).name = f"Heightmap Relief ({shape})"
            finally:
                _app.activeViewport.isUpdateLocked = False
                base_feat.finishEdit()

            try: os.remove(stl_fp.name)
            except OSError: pass

            # Створення твердотільної BRep основи
            if create_solid_base and solid_base_thickness > 0:
                progress.progressValue = 94
                progress.message = "Створення твердотільної BRep основи з CAM-інструментами..."
                try:
                    sketches = root_comp.sketches
                    xy_plane = root_comp.xYConstructionPlane
                    sketch = sketches.add(xy_plane)

                    sketch.isComputeDeferred = True
                    try:
                        p_shift_x = shift_x / 10.0
                        p_shift_y = shift_y / 10.0

                        if shape == 'Прямокутник':
                            p0 = adsk.core.Point3D.create(p_shift_x, p_shift_y, shift_z / 10.0)
                            p1 = adsk.core.Point3D.create(p_shift_x + width_mm / 10.0, p_shift_y + height_mm / 10.0, shift_z / 10.0)
                            sketch.sketchCurves.sketchLines.addTwoPointRectangle(p0, p1)

                            # Монтажні отвори під гвинти у 4 кутах
                            if create_holes and hole_offset > 0:
                                r_h_cm = (hole_dia_mm / 2.0) / 10.0
                                off_cm = hole_offset / 10.0
                                w_cm = width_mm / 10.0
                                l_cm = height_mm / 10.0
                                h_centers = [
                                    (p_shift_x + off_cm, p_shift_y + off_cm),
                                    (p_shift_x + w_cm - off_cm, p_shift_y + off_cm),
                                    (p_shift_x + off_cm, p_shift_y + l_cm - off_cm),
                                    (p_shift_x + w_cm - off_cm, p_shift_y + l_cm - off_cm)
                                ]
                                for hc_x, hc_y in h_centers:
                                    sketch.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(hc_x, hc_y, shift_z / 10.0), r_h_cm)

                        elif shape == 'Коло':
                            cx_cm = p_shift_x + (width_mm / 2.0) / 10.0
                            cy_cm = p_shift_y + (height_mm / 2.0) / 10.0
                            r_cm = (width_mm / 2.0) / 10.0
                            center = adsk.core.Point3D.create(cx_cm, cy_cm, shift_z / 10.0)
                            sketch.sketchCurves.sketchCircles.addByCenterRadius(center, r_cm)
                            if create_holes and hole_offset > 0:
                                r_h_cm = (hole_dia_mm / 2.0) / 10.0
                                hole_dist_cm = max(0.1, r_cm - (hole_offset / 10.0))
                                for angle_deg in [45, 135, 225, 315]:
                                    ang_rad = math.radians(angle_deg)
                                    hx = cx_cm + hole_dist_cm * math.cos(ang_rad)
                                    hy = cy_cm + hole_dist_cm * math.sin(ang_rad)
                                    sketch.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(hx, hy, shift_z / 10.0), r_h_cm)

                        elif shape in ('Овал', 'Коло / Овал'):
                            cx_cm = p_shift_x + (width_mm / 2.0) / 10.0
                            cy_cm = p_shift_y + (height_mm / 2.0) / 10.0
                            rx_cm = (width_mm / 2.0) / 10.0
                            ry_cm = (height_mm / 2.0) / 10.0
                            center = adsk.core.Point3D.create(cx_cm, cy_cm, shift_z / 10.0)
                            if abs(rx_cm - ry_cm) < 1e-5:
                                sketch.sketchCurves.sketchCircles.addByCenterRadius(center, rx_cm)
                            else:
                                major_pt = adsk.core.Point3D.create(cx_cm + rx_cm, cy_cm, shift_z / 10.0)
                                point_on = adsk.core.Point3D.create(cx_cm, cy_cm + ry_cm, shift_z / 10.0)
                                sketch.sketchCurves.sketchEllipses.add(center, major_pt, point_on)
                            if create_holes and hole_offset > 0:
                                r_h_cm = (hole_dia_mm / 2.0) / 10.0
                                off_x_cm = max(0.1, rx_cm - (hole_offset / 10.0))
                                off_y_cm = max(0.1, ry_cm - (hole_offset / 10.0))
                                for sx, sy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
                                    hx = cx_cm + sx * off_x_cm * 0.7071
                                    hy = cy_cm + sy * off_y_cm * 0.7071
                                    sketch.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(hx, hy, shift_z / 10.0), r_h_cm)
                        elif shape == 'Багатокутник':
                            sides = int_val('polySides')
                            radius = val_mm('polyRadius')
                            poly = make_regular_polygon(radius, radius, sides, radius)
                            lines = sketch.sketchCurves.sketchLines
                            for i in range(len(poly)):
                                p0 = adsk.core.Point3D.create(p_shift_x + poly[i][0] / 10.0, p_shift_y + poly[i][1] / 10.0, shift_z / 10.0)
                                p1 = adsk.core.Point3D.create(p_shift_x + poly[(i + 1) % len(poly)][0] / 10.0, p_shift_y + poly[(i + 1) % len(poly)][1] / 10.0, shift_z / 10.0)
                                lines.addByTwoPoints(p0, p1)
                        else:
                            loops = chain_boundary_edges(boundary_edges, vertices)
                            lines = sketch.sketchCurves.sketchLines
                            eps = max(0.2, vertex_spacing * 0.4)
                            for loop in loops:
                                simp = simplify_closed_loop(loop, epsilon=eps)
                                if len(simp) >= 3:
                                    for i in range(len(simp)):
                                        p0 = adsk.core.Point3D.create(simp[i][0] / 10.0, simp[i][1] / 10.0, shift_z / 10.0)
                                        p1 = adsk.core.Point3D.create(simp[(i + 1) % len(simp)][0] / 10.0, simp[(i + 1) % len(simp)][1] / 10.0, shift_z / 10.0)
                                        lines.addByTwoPoints(p0, p1)
                    finally:
                        sketch.isComputeDeferred = False

                    if sketch.profiles.count > 0:
                        prof_coll = adsk.core.ObjectCollection.create()
                        for p in sketch.profiles:
                            prof_coll.add(p)

                        ext_input = root_comp.features.extrudeFeatures.createInput(
                            prof_coll, adsk.fusion.FeatureOperations.NewBodyFeatureOperation
                        )
                        ext_distance = adsk.core.ValueInput.createByReal(-solid_base_thickness / 10.0)
                        ext_input.setDistanceExtent(False, ext_distance)
                        ext_feat = root_comp.features.extrudeFeatures.add(ext_input)
                        if ext_feat.bodies.count > 0:
                            solid_body = ext_feat.bodies.item(0)
                            solid_body.name = "SolidBase_Plate"

                            # Накладання фаски або скруглення на краї основи
                            if edge_treatment != 'Немає' and edge_radius > 0:
                                edge_coll = adsk.core.ObjectCollection.create()
                                for edge in solid_body.edges:
                                    edge_coll.add(edge)

                                if edge_treatment == 'Скруглення (Fillet)':
                                    fillet_input = root_comp.features.filletFeatures.createInput()
                                    fillet_input.addConstantRadiusEdgeSet(edge_coll, adsk.core.ValueInput.createByReal(edge_radius / 10.0), True)
                                    root_comp.features.filletFeatures.add(fillet_input)
                                elif edge_treatment == 'Фаска (Chamfer)':
                                    chamfer_input = root_comp.features.chamferFeatures.createInput2()
                                    chamfer_input.chamferEdgeSets.addEqualDistanceChamferEdgeSet(edge_coll, adsk.core.ValueInput.createByReal(edge_radius / 10.0), True)
                                    root_comp.features.chamferFeatures.add(chamfer_input)

                except Exception as ex_solid:
                    if _ui:
                        _ui.messageBox(
                            f"Попередження: Не вдалося створити твердотільну BRep основу або обробити її краї.\n\n"
                            f"Причина: {str(ex_solid)}\n\n"
                            "Меш-модель рельєфу була успішно створена.",
                            "Попередження створення основи",
                            adsk.core.MessageBoxButtonTypes.OKButtonType,
                            adsk.core.MessageBoxIconTypes.WarningIconType
                        )

            progress.progressValue = 100
            progress.message = "Завершено!"

            if mesh_list.count > 0:
                _app.activeViewport.fit()
                solid_msg = f"Тверда BRep плита з отворами: {solid_base_thickness:.2f} мм\n" if create_solid_base else ""
                fade_msg = f"Плавне згасання країв: {fade_width:.1f} мм\n" if enable_vignette else ""
                holes_msg = f"Отвори: 4x {hole_type} (відступ {hole_offset:.1f} мм)\n" if create_solid_base and create_holes else ""
                backend_msg = "NumPy ⚡ (Binary STL)" if HAS_NUMPY else "Python Fallback (Binary STL)"
                _ui.messageBox(
                    "Готово! MeshBody успішно створено.\n"
                    f"Двигун: {backend_msg}\n"
                    f"Початок координат: {origin_mode}\n"
                    f"Форма: {shape}\n"
                    f"Гамма: {gamma_val:.2f} | Інверсія: {'Так' if invert_height else 'Ні'}\n"
                    f"Висота підкладки: {base_thickness:.2f} мм | Глибина рельєфу: {max_depth:.2f} мм\n"
                    f"{fade_msg}{solid_msg}{holes_msg}"
                    f"Сітка: {grid_cols} x {grid_rows} | Вершин: {len(vertices):,} | Трикутників: {len(triangles):,}"
                )
            else:
                _ui.messageBox('Не вдалося створити MeshBody.')

        except:
            if _ui:
                _ui.messageBox('Помилка виконання:\n{}'.format(traceback.format_exc()))
        finally:
            if progress:
                progress.hide()

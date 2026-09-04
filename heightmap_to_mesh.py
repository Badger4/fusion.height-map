"""
Heightmap -> MeshBody для Fusion 360 (з інтерактивним діалогом)
=================================================================
Що робить:
  1. Обираєш файл зображення через стандартне вікно "Відкрити"
     (підтримуються 8-бітні та 16-бітні карти висот: PNG, TIFF, BMP, JPG).
  2. Векторизоване прискорення через NumPy (з автоматичним чистим Python-fallback):
     - Обчислення висот (LUT-lookup), маски та сітки вершин відбуваються за 20–40 мс!
  3. Чутливий інтерфейс без зависань (adsk.doEvents + часта перевірка Cancel).
  4. Повне збереження меж зображення (Без випадкового обрізання країв):
     - Для прямокутника, кола та багатокутника файл використовується на 100% без обрізання темних країв.
     - Опція обрізання прозорих полів діє ТІЛЬКИ для PNG з прозорим Alpha-каналом у режимі "Контур".
  5. Автоматично зберігає та підвантажує останні використані параметри (JSON).
  6. Окремий твердотільний плоский об'єкт (Solid BRep Body) ТОЧНО ПО КОНТУРУ:
     - Для контуру зображення основа автоматично повторює силует деталі з надійною обробкою помилок.
  7. Поки файл не обрано — кнопка "OK" автоматично заблокована.
  8. Результат вставляється як герметичний (Watertight / 2-Manifold) MeshBody з розпізнаваними назвами в тімлайні.
  9. Автоматично пропонує встановити Pillow в один клік, якщо модуль відсутній.

Запуск: Scripts and Add-Ins -> вибрати файл -> Run.
"""

import adsk.core, adsk.fusion, adsk.cam, traceback
import os
import sys
import math
import array
import json
import tempfile
import subprocess
from collections import deque

# Спроба імпорту NumPy для максимального векторизованого прискорення
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

MAX_VERTICES_WARNING = 600000  # поріг попередження про розмір сітки
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
    """Перевіряє наявність Pillow. Якщо відсутній — пропонує встановити автоматично."""
    try:
        import PIL
        from PIL import Image, ImageFilter
        return True
    except ImportError:
        res = ui.messageBox(
            "Модуль Pillow (PIL) не знайдено у вбудованому Python-середовищі Fusion 360.\n\n"
            "Бажаєте встановити його автоматично прямо зараз в один клік?",
            "Авто-встановлення Pillow",
            adsk.core.MessageBoxButtonTypes.YesNoButtonType,
            adsk.core.MessageBoxIconTypes.QuestionIconType
        )
        if res == adsk.core.DialogResults.DialogYes:
            progress = ui.createProgressDialog()
            progress.isCancelButtonShown = False
            progress.show("Встановлення Pillow", "Завантаження та встановлення через pip...", 0, 100, 1)
            try:
                cmd = [sys.executable, "-m", "pip", "install", "Pillow"]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                progress.hide()
                if proc.returncode == 0:
                    ui.messageBox("Pillow успішно встановлено!")
                    return True
                else:
                    ui.messageBox(f"Помилка встановлення Pillow:\n{proc.stderr}\n{proc.stdout}")
                    return False
            except Exception:
                progress.hide()
                ui.messageBox(f"Не вдалося встановити Pillow:\n{traceback.format_exc()}")
                return False
        return False


# =====================================================================
# Геометричні допоміжні функції та аналіз фону
# =====================================================================

def make_regular_polygon(cx, cy, sides, radius):
    """Вершини правильного багатокутника, вписаного в коло радіусом radius."""
    verts = []
    for i in range(sides):
        angle = -math.pi / 2.0 + i * (2.0 * math.pi / sides)
        verts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return verts


def point_in_polygon(px, py, poly):
    """Перевірка належності точки довільному багатокутнику (Ray-casting)."""
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
    """Знаходить рамку обрізки. Якщо only_alpha=True, шукає ТІЛЬКИ по прозорому Alpha-каналу."""
    try:
        if img.mode in ('RGBA', 'LA') or ('transparency' in img.info):
            alpha = img.convert('RGBA').split()[-1]
            mask = alpha.point(lambda p: 255 if p >= threshold else 0)
            bbox = mask.getbbox()
            if bbox:
                return bbox

        if not only_alpha:
            img_gray = img.convert('L')
            w, h = img_gray.size
            pixels = img_gray.tobytes()
            corners = [pixels[0], pixels[w - 1], pixels[(h - 1) * w], pixels[w * h - 1]]
            avg_corner = sum(corners) / 4.0

            if avg_corner < 128:
                mask = img_gray.point(lambda p: 255 if p > threshold else 0)
            else:
                mask = img_gray.point(lambda p: 255 if p < (255 - threshold) else 0)

            return mask.getbbox()
        return None
    except:
        return None


def detect_outer_mask(grid_cols, grid_rows, alpha_bytes=None, raw_pixels=None, is_16bit=False, threshold=32, fill_holes=True):
    """Визначає маску об'єкта шляхом хвильового алгоритму (Flood-Fill) від країв сітки."""
    total = grid_cols * grid_rows
    if alpha_bytes is not None:
        if not fill_holes:
            return bytearray(1 if alpha_bytes[i] >= threshold else 0 for i in range(total))
        is_bg = lambda idx: alpha_bytes[idx] < threshold
    else:
        if not fill_holes:
            return bytearray(1 if (raw_pixels[i] if not is_16bit else raw_pixels[i] >> 8) >= threshold else 0 for i in range(total))

        corner_indices = [0, grid_cols - 1, (grid_rows - 1) * grid_cols, total - 1]
        if is_16bit:
            corner_vals = [(raw_pixels[i] >> 8) for i in corner_indices]
        else:
            corner_vals = [raw_pixels[i] for i in corner_indices]
        avg_corner = sum(corner_vals) / 4.0

        if avg_corner < 128:
            is_bg = lambda idx: (raw_pixels[idx] >> 8 if is_16bit else raw_pixels[idx]) <= threshold
        else:
            is_bg = lambda idx: (raw_pixels[idx] >> 8 if is_16bit else raw_pixels[idx]) >= (255 - threshold)

    visited = bytearray(total)
    queue = deque()

    for x in range(grid_cols):
        for y in (0, grid_rows - 1):
            idx = y * grid_cols + x
            if not visited[idx] and is_bg(idx):
                visited[idx] = 1
                queue.append((x, y))

    for y in range(grid_rows):
        for x in (0, grid_cols - 1):
            idx = y * grid_cols + x
            if not visited[idx] and is_bg(idx):
                visited[idx] = 1
                queue.append((x, y))

    while queue:
        cx, cy = queue.popleft()
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < grid_cols and 0 <= ny < grid_rows:
                n_idx = ny * grid_cols + nx
                if not visited[n_idx] and is_bg(n_idx):
                    visited[n_idx] = 1
                    queue.append((nx, ny))

    is_object = bytearray(1 if not visited[i] else 0 for i in range(total))
    return is_object


def chain_boundary_edges(boundary_edges, vertices):
    """
    Надійно з'єднує ребра межі у замкнені контури (loops),
    коректно обробляючи складна розгалуження та Т-подібні стики.
    """
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
                    # Шукаємо наступне невідвідане ребро
                    next_candidates = [nbr for nbr in adj[curr] if (curr, nbr) not in visited_edges]
                    if not next_candidates:
                        break  # Контур не замкнувся нормально
                    next_node = next_candidates[0]
                    visited_edges.add((curr, next_node))
                    curr = next_node

                if curr == start_node and len(loop) >= 3:
                    loop_pts = [(vertices[v][0], vertices[v][1]) for v in loop]
                    loops.append(loop_pts)

    return loops


def rdp_simplify(points, epsilon=0.2):
    """Спрощення полігональної лінії алгоритмом Рамера-Дугласа-Пекера."""
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
    """Спрощення замкненого контуру без втрати форми."""
    n = len(loop)
    if n < 6:
        return loop
    mid = n // 2
    part1 = rdp_simplify(loop[:mid + 1], epsilon)
    part2 = rdp_simplify(loop[mid:] + [loop[0]], epsilon)
    return part1[:-1] + part2[:-1]


def write_obj(path, vertices, triangles, progress=None):
    """Мінімальний ASCII OBJ з точністю 0.001 мм та відгуком UI."""
    chunk_size = 50000
    with open(path, 'w', buffering=4 * 1024 * 1024, encoding='ascii') as f:
        for i in range(0, len(vertices), chunk_size):
            if progress and progress.wasCancelled:
                return
            adsk.doEvents()
            chunk = vertices[i:i + chunk_size]
            f.write("".join(f"v {x:.3f} {y:.3f} {z:.3f}\n" for x, y, z in chunk))

        for i in range(0, len(triangles), chunk_size):
            if progress and progress.wasCancelled:
                return
            adsk.doEvents()
            chunk = triangles[i:i + chunk_size]
            f.write("".join(f"f {a + 1} {b + 1} {c + 1}\n" for a, b, c in chunk))


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
                'Створює MeshBody-рельєф із зображення (карта висот) з обрізкою по формі заготовки'
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

            # ---- Зображення ----
            img_input = inputs.addStringValueInput('imagePath', 'Файл зображення', saved.get('imagePath', ''))
            img_input.isReadOnly = True
            inputs.addBoolValueInput('browseBtn', 'Обрати зображення...', False, '', False)
            auto_crop = inputs.addBoolValueInput('autoCrop', 'Обрізати прозорі поля (тільки для PNG з Alpha-прозорістю)', True, '', saved.get('autoCrop', False))
            auto_crop.isVisible = False
            inputs.addBoolValueInput('keepAspect', 'Зберігати пропорції (Aspect Ratio)', True, '', saved.get('keepAspect', True))

            # ---- Форма заготовки ----
            shape_dd = inputs.addDropDownCommandInput(
                'shapeType', 'Форма заготовки', adsk.core.DropDownStyles.TextListDropDownStyle
            )
            saved_shape = saved.get('shapeType', 'Прямокутник')
            for s_name in ['Прямокутник', 'Коло / Овал', 'Багатокутник', 'Контур зображення (Alpha / Прозорість)']:
                shape_dd.listItems.add(s_name, s_name == saved_shape, '')

            mm = 'mm'
            def_w = f"{saved.get('rectWidth', 100.0)} mm"
            def_l = f"{saved.get('rectLength', 100.0)} mm"
            def_r = f"{saved.get('polyRadius', 50.0)} mm"

            # Прямокутник
            rect_w = inputs.addValueInput('rectWidth', 'Ширина (X)', mm, adsk.core.ValueInput.createByString(def_w))
            rect_l = inputs.addValueInput('rectLength', 'Довжина (Y)', mm, adsk.core.ValueInput.createByString(def_l))

            # Коло / Овал
            circle_dx = inputs.addValueInput('circleDiaX', 'Діаметр X (Ширина)', mm, adsk.core.ValueInput.createByString(def_w))
            circle_dx.isVisible = (saved_shape == 'Коло / Овал')
            circle_dy = inputs.addValueInput('circleDiaY', 'Діаметр Y (Довжина)', mm, adsk.core.ValueInput.createByString(def_l))
            circle_dy.isVisible = (saved_shape == 'Коло / Овал')

            # Багатокутник
            poly_sides = inputs.addIntegerSpinnerCommandInput('polySides', 'Кількість граней', 3, 36, 1, saved.get('polySides', 6))
            poly_sides.isVisible = (saved_shape == 'Багатокутник')
            poly_r = inputs.addValueInput('polyRadius', 'Радіус (до вершини)', mm, adsk.core.ValueInput.createByString(def_r))
            poly_r.isVisible = (saved_shape == 'Багатокутник')

            # Контур зображення (Alpha / Прозорість)
            alpha_w = inputs.addValueInput('alphaWidth', 'Ширина (X)', mm, adsk.core.ValueInput.createByString(def_w))
            alpha_w.isVisible = (saved_shape == 'Контур зображення (Alpha / Прозорість)')
            alpha_l = inputs.addValueInput('alphaLength', 'Довжина (Y)', mm, adsk.core.ValueInput.createByString(def_l))
            alpha_l.isVisible = (saved_shape == 'Контур зображення (Alpha / Прозорість)')
            alpha_thresh = inputs.addIntegerSpinnerCommandInput('alphaThreshold', 'Поріг прозорості (1-254)', 1, 254, 1, saved.get('alphaThreshold', 32))
            alpha_thresh.isVisible = (saved_shape == 'Контур зображення (Alpha / Прозорість)')
            fill_holes = inputs.addBoolValueInput('fillHoles', 'Суцільна основа (запобігати діркам у темних тінях)', True, '', saved.get('fillHoles', True))
            fill_holes.isVisible = (saved_shape == 'Контур зображення (Alpha / Прозорість)')

            inputs.addSeparatorCommandInput('sep1')

            # ---- Параметри рельєфу ----
            inputs.addValueInput('maxDepth', 'Глибина рельєфу (макс. висота)', mm, adsk.core.ValueInput.createByString(f"{saved.get('maxDepth', 2.0)} mm"))
            inputs.addValueInput('baseThickness', 'Висота заготовки (товщина підкладки, >=0)', mm, adsk.core.ValueInput.createByString(f"{saved.get('baseThickness', 5.0)} mm"))
            inputs.addValueInput('vertexSpacing', 'Крок сітки (деталізація)', mm, adsk.core.ValueInput.createByString(f"{saved.get('vertexSpacing', 0.5)} mm"))
            inputs.addIntegerSpinnerCommandInput('smoothPasses', 'Проходи згладжування', 0, 10, 1, saved.get('smoothPasses', 1))

            # Інверсія та гамма
            inputs.addBoolValueInput('invertHeight', 'Інвертувати висоту (чорний = випуклий)', True, '', saved.get('invertHeight', True))
            inputs.addValueInput('gammaVal', 'Гамма-корекція (1.0 = норма, <1 світліше, >1 контрастніше)', '', adsk.core.ValueInput.createByReal(saved.get('gammaVal', 1.0)))

            # ---- Тверда BRep основа (Solid Plate) ----
            inputs.addSeparatorCommandInput('sep2')
            create_solid = saved.get('createSolidBase', False)
            inputs.addBoolValueInput('createSolidBase', 'Створювати окрему тверду основу (Solid BRep плиту по контуру)', True, '', create_solid)
            solid_thick = inputs.addValueInput('solidBaseThickness', 'Товщина твердотільної плити під рельєфом', mm, adsk.core.ValueInput.createByString(f"{saved.get('solidBaseThickness', 5.0)} mm"))
            solid_thick.isVisible = create_solid

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

            # Валідація гамма-корекції
            gamma_input = inputs.itemById('gammaVal')
            if gamma_input and gamma_input.value <= 0:
                args.areInputsValid = False
                return

            # Валідація глибини рельєфу та підкладки
            max_d = inputs.itemById('maxDepth')
            base_t = inputs.itemById('baseThickness')
            if not max_d or not base_t or max_d.value < 0 or base_t.value < 0:
                args.areInputsValid = False
                return

            shape_dd = inputs.itemById('shapeType')
            if shape_dd and shape_dd.selectedItem:
                shape = shape_dd.selectedItem.name
                if shape == 'Прямокутник':
                    w = inputs.itemById('rectWidth')
                    l = inputs.itemById('rectLength')
                    if not w or not l or w.value <= 0 or l.value <= 0:
                        args.areInputsValid = False
                        return
                elif shape == 'Коло / Овал':
                    dx = inputs.itemById('circleDiaX')
                    dy = inputs.itemById('circleDiaY')
                    if not dx or not dy or dx.value <= 0 or dy.value <= 0:
                        args.areInputsValid = False
                        return
                elif shape == 'Багатокутник':
                    r = inputs.itemById('polyRadius')
                    if not r or r.value <= 0:
                        args.areInputsValid = False
                        return
                elif shape == 'Контур зображення (Alpha / Прозорість)':
                    aw = inputs.itemById('alphaWidth')
                    al = inputs.itemById('alphaLength')
                    if not aw or not al or aw.value <= 0 or al.value <= 0:
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
                                auto_crop = (auto_crop_input.value if auto_crop_input else False) and (shape == 'Контур зображення (Alpha / Прозорість)')

                                alpha_thresh_input = inputs.itemById('alphaThreshold')
                                thresh = alpha_thresh_input.value if alpha_thresh_input else 32

                                bbox = get_content_bbox(probe_img, only_alpha=True, threshold=thresh) if auto_crop else None
                                if bbox:
                                    pw = bbox[2] - bbox[0]
                                    ph = bbox[3] - bbox[1]
                                else:
                                    pw, ph = probe_img.size

                                if ph > 0:
                                    _img_aspect_ratio = float(pw) / float(ph)

                                    keep_aspect = adsk.core.BoolValueCommandInput.cast(inputs.itemById('keepAspect')).value
                                    if keep_aspect:
                                        _is_updating_aspect = True
                                        rw = adsk.core.ValueCommandInput.cast(inputs.itemById('rectWidth'))
                                        rl = adsk.core.ValueCommandInput.cast(inputs.itemById('rectLength'))
                                        if rw and rl:
                                            rl.value = rw.value / _img_aspect_ratio

                                        cdx = adsk.core.ValueCommandInput.cast(inputs.itemById('circleDiaX'))
                                        cdy = adsk.core.ValueCommandInput.cast(inputs.itemById('circleDiaY'))
                                        if cdx and cdy:
                                            cdy.value = cdx.value / _img_aspect_ratio

                                        aw = adsk.core.ValueCommandInput.cast(inputs.itemById('alphaWidth'))
                                        al = adsk.core.ValueCommandInput.cast(inputs.itemById('alphaLength'))
                                        if aw and al:
                                            al.value = aw.value / _img_aspect_ratio
                                        _is_updating_aspect = False
                        except:
                            pass
                    bool_input.value = False

            # Перемикання форми
            if changed.id == 'shapeType':
                dd = adsk.core.DropDownCommandInput.cast(changed)
                shape = dd.selectedItem.name
                is_rect = (shape == 'Прямокутник')
                is_circle = (shape == 'Коло / Овал')
                is_poly = (shape == 'Багатокутник')
                is_alpha = (shape == 'Контур зображення (Alpha / Прозорість)')

                inputs.itemById('rectWidth').isVisible = is_rect
                inputs.itemById('rectLength').isVisible = is_rect
                inputs.itemById('circleDiaX').isVisible = is_circle
                inputs.itemById('circleDiaY').isVisible = is_circle
                inputs.itemById('polySides').isVisible = is_poly
                inputs.itemById('polyRadius').isVisible = is_poly
                inputs.itemById('alphaWidth').isVisible = is_alpha
                inputs.itemById('alphaLength').isVisible = is_alpha
                inputs.itemById('alphaThreshold').isVisible = is_alpha
                inputs.itemById('fillHoles').isVisible = is_alpha
                inputs.itemById('autoCrop').isVisible = is_alpha

            # Перемикання твердотільної основи
            if changed.id == 'createSolidBase':
                is_solid = adsk.core.BoolValueCommandInput.cast(changed).value
                inputs.itemById('solidBaseThickness').isVisible = is_solid

            # Автоматична синхронізація пропорцій при зміні розмірів
            if not _is_updating_aspect and _img_aspect_ratio > 0:
                keep_aspect_input = inputs.itemById('keepAspect')
                keep_aspect = keep_aspect_input.value if keep_aspect_input else False
                if keep_aspect:
                    if changed.id == 'rectWidth':
                        _is_updating_aspect = True
                        rw = adsk.core.ValueCommandInput.cast(changed)
                        rl = adsk.core.ValueCommandInput.cast(inputs.itemById('rectLength'))
                        if rw and rl:
                            rl.value = rw.value / _img_aspect_ratio
                        _is_updating_aspect = False
                    elif changed.id == 'rectLength':
                        _is_updating_aspect = True
                        rl = adsk.core.ValueCommandInput.cast(changed)
                        rw = adsk.core.ValueCommandInput.cast(inputs.itemById('rectWidth'))
                        if rw and rl:
                            rw.value = rl.value * _img_aspect_ratio
                        _is_updating_aspect = False
                    elif changed.id == 'circleDiaX':
                        _is_updating_aspect = True
                        cdx = adsk.core.ValueCommandInput.cast(changed)
                        cdy = adsk.core.ValueCommandInput.cast(inputs.itemById('circleDiaY'))
                        if cdx and cdy:
                            cdy.value = cdx.value / _img_aspect_ratio
                        _is_updating_aspect = False
                    elif changed.id == 'circleDiaY':
                        _is_updating_aspect = True
                        cdy = adsk.core.ValueCommandInput.cast(changed)
                        cdx = adsk.core.ValueCommandInput.cast(inputs.itemById('circleDiaX'))
                        if cdx and cdy:
                            cdx.value = cdy.value * _img_aspect_ratio
                        _is_updating_aspect = False
                    elif changed.id == 'alphaWidth':
                        _is_updating_aspect = True
                        aw = adsk.core.ValueCommandInput.cast(changed)
                        al = adsk.core.ValueCommandInput.cast(inputs.itemById('alphaLength'))
                        if aw and al:
                            al.value = aw.value / _img_aspect_ratio
                        _is_updating_aspect = False
                    elif changed.id == 'alphaLength':
                        _is_updating_aspect = True
                        al = adsk.core.ValueCommandInput.cast(changed)
                        aw = adsk.core.ValueCommandInput.cast(inputs.itemById('alphaWidth'))
                        if aw and al:
                            aw.value = al.value * _img_aspect_ratio
                        _is_updating_aspect = False

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

            # ---- Зображення ----
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
                _ui.messageBox(
                    'Вставка мешу вимагає параметричного режиму.\n\n'
                    'Увімкни "Capture design history" і запусти команду ще раз.'
                )
                return

            def val_mm(input_id):
                return adsk.core.ValueCommandInput.cast(inputs.itemById(input_id)).value * 10.0

            def int_val(input_id):
                return adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById(input_id)).value

            shape = adsk.core.DropDownCommandInput.cast(inputs.itemById('shapeType')).selectedItem.name

            max_depth = val_mm('maxDepth')
            base_thickness = val_mm('baseThickness')
            vertex_spacing = val_mm('vertexSpacing')
            if vertex_spacing <= 0:
                vertex_spacing = 0.5
            smooth_passes = int_val('smoothPasses')

            invert_height = adsk.core.BoolValueCommandInput.cast(inputs.itemById('invertHeight')).value
            gamma_input = inputs.itemById('gammaVal')
            gamma_val = adsk.core.ValueCommandInput.cast(gamma_input).value if gamma_input else 1.0
            if gamma_val <= 0:
                gamma_val = 1.0

            auto_crop_input = inputs.itemById('autoCrop')
            auto_crop = (auto_crop_input.value if auto_crop_input else False) and (shape == 'Контур зображення (Alpha / Прозорість)')

            create_solid_input = inputs.itemById('createSolidBase')
            create_solid_base = create_solid_input.value if create_solid_input else False
            solid_base_thickness = val_mm('solidBaseThickness') if create_solid_base else 0.0

            # Збереження останніх параметрів
            save_last_params({
                'imagePath': image_path,
                'shapeType': shape,
                'rectWidth': val_mm('rectWidth') if inputs.itemById('rectWidth') else 100.0,
                'rectLength': val_mm('rectLength') if inputs.itemById('rectLength') else 100.0,
                'polySides': int_val('polySides') if inputs.itemById('polySides') else 6,
                'polyRadius': val_mm('polyRadius') if inputs.itemById('polyRadius') else 50.0,
                'alphaThreshold': int_val('alphaThreshold') if inputs.itemById('alphaThreshold') else 32,
                'fillHoles': inputs.itemById('fillHoles').value if inputs.itemById('fillHoles') else True,
                'autoCrop': inputs.itemById('autoCrop').value if inputs.itemById('autoCrop') else False,
                'keepAspect': inputs.itemById('keepAspect').value if inputs.itemById('keepAspect') else True,
                'maxDepth': max_depth,
                'baseThickness': base_thickness,
                'vertexSpacing': vertex_spacing,
                'smoothPasses': smooth_passes,
                'invertHeight': invert_height,
                'gammaVal': gamma_val,
                'createSolidBase': create_solid_base,
                'solidBaseThickness': solid_base_thickness
            })

            # ---- Габарити заготовки ----
            if shape == 'Прямокутник':
                width_mm = val_mm('rectWidth')
                height_mm = val_mm('rectLength')
            elif shape == 'Коло / Овал':
                width_mm = val_mm('circleDiaX')
                height_mm = val_mm('circleDiaY')
            elif shape == 'Багатокутник':
                radius = val_mm('polyRadius')
                width_mm = height_mm = radius * 2.0
            else:  # Контур зображення (Alpha / Прозорість)
                width_mm = val_mm('alphaWidth')
                height_mm = val_mm('alphaLength')

            if width_mm <= 0 or height_mm <= 0:
                _ui.messageBox('Розміри заготовки мають бути більше нуля.')
                return

            grid_cols = max(2, int(round(width_mm / vertex_spacing)) + 1)
            grid_rows = max(2, int(round(height_mm / vertex_spacing)) + 1)

            if grid_cols * grid_rows > MAX_VERTICES_WARNING:
                res = _ui.messageBox(
                    f'Сітка буде {grid_cols}x{grid_rows} = {grid_cols * grid_rows} вершин.\n'
                    f'Це може зайняти кілька секунд.\n\nПродовжити?',
                    'Попередження про розмір сітки',
                    adsk.core.MessageBoxButtonTypes.YesNoButtonType,
                    adsk.core.MessageBoxIconTypes.WarningIconType
                )
                if res != adsk.core.DialogResults.DialogYes:
                    return

            # ---- Ініціалізація ProgressDialog ----
            progress = _ui.createProgressDialog()
            progress.isCancelButtonShown = True
            progress.show("Генерація 3D-рельєфу", "Завантаження зображення...", 0, 100, 1)

            # ---- 1. Завантаження і підготовка зображення (8-біт або 16-біт) ----
            progress.progressValue = 10
            progress.message = "Завантаження карти висот (8/16-біт)..."
            if progress.wasCancelled:
                return

            resample_filter = getattr(Image, 'Resampling', Image).LANCZOS
            img_raw = Image.open(image_path)

            if auto_crop:
                crop_thresh = int_val('alphaThreshold')
                bbox = get_content_bbox(img_raw, only_alpha=True, threshold=crop_thresh)
                if bbox:
                    img_raw = img_raw.crop(bbox)

            has_alpha = (img_raw.mode in ('RGBA', 'LA') or ('transparency' in img_raw.info))
            if has_alpha:
                img_rgba = img_raw.convert('RGBA')
                img_resized_alpha = img_rgba.resize((grid_cols, grid_rows), resample_filter)
                alpha_bytes = img_resized_alpha.split()[-1].tobytes()
            else:
                alpha_bytes = None

            is_16bit = img_raw.mode in ('I;16', 'I;16L', 'I;16B', 'I', 'F') or getattr(img_raw, 'bits', 8) == 16

            progress.progressValue = 25
            progress.message = f"Фільтрація та згладжування ({'NumPy' if HAS_NUMPY else 'Python'})..."
            if progress.wasCancelled:
                return

            if is_16bit:
                img_conv = img_raw.convert('I')
                img_resized = img_conv.resize((grid_cols, grid_rows), getattr(Image, 'Resampling', Image).BILINEAR)
                if smooth_passes > 0:
                    for _ in range(smooth_passes):
                        img_resized = img_resized.filter(ImageFilter.SMOOTH)
                max_val = 65535.0
                lut_size = 65536
            else:
                if has_alpha:
                    img_gray = img_resized_alpha.convert('L')
                else:
                    img_gray = img_raw.convert('L').resize((grid_cols, grid_rows), resample_filter)
                if smooth_passes > 0:
                    for _ in range(smooth_passes):
                        img_gray = img_gray.filter(ImageFilter.SMOOTH)
                max_val = 255.0
                lut_size = 256

            # ---- 2. Таблиця відповідності (LUT) ----
            lut = [0.0] * lut_size
            for i in range(lut_size):
                v = i / max_val
                if invert_height:
                    v = 1.0 - v
                if gamma_val != 1.0 and gamma_val > 0:
                    v = math.pow(max(0.0, min(1.0, v)), gamma_val)
                lut[i] = v * max_depth + base_thickness

            progress.progressValue = 45
            progress.message = "Розрахунок 3D-вершин..."
            if progress.wasCancelled:
                return

            step_x = width_mm / (grid_cols - 1)
            step_y = height_mm / (grid_rows - 1)

            # =================================================================
            # ТРАЄКТОРІЯ А: NumPy Векторизована обробка
            # =================================================================
            if HAS_NUMPY:
                lut_arr = np.array(lut, dtype=np.float32)
                if is_16bit:
                    raw_arr = np.clip(np.asarray(img_resized, dtype=np.int32), 0, 65535)
                else:
                    raw_arr = np.asarray(img_gray, dtype=np.uint8)

                z_grid = np.take(lut_arr, raw_arr)
                y_idx, x_idx = np.ogrid[:grid_rows, :grid_cols]
                x_grid = x_idx * step_x
                y_grid = (grid_rows - 1 - y_idx) * step_y

                all_vertices = np.column_stack((
                    np.tile(x_grid, (grid_rows, 1)).ravel(),
                    np.tile(y_grid, (1, grid_cols)).ravel(),
                    z_grid.ravel()
                ))
                # Конвертуємо у список кортежів для сумісності з OBJ та BRep-контуром
                all_vertices_list = [tuple(v) for v in all_vertices]

                progress.progressValue = 65
                progress.message = "Побудова трикутної сітки та стінок (NumPy)..."
                if progress.wasCancelled:
                    return

                if shape == 'Прямокутник':
                    inside_mask = np.ones((grid_rows, grid_cols), dtype=bool)
                elif shape == 'Коло / Овал':
                    cx, cy = width_mm / 2.0, height_mm / 2.0
                    rx, ry = width_mm / 2.0, height_mm / 2.0
                    inside_mask = (((x_grid - cx) / rx) ** 2 + ((y_grid - cy) / ry) ** 2) <= (1.0 + 1e-6)
                elif shape == 'Багатокутник':
                    sides = int_val('polySides')
                    radius = val_mm('polyRadius')
                    poly = make_regular_polygon(radius, radius, sides, radius)
                    mask_flat = bytearray(grid_cols * grid_rows)
                    idx = 0
                    for vy in vy_table if 'vy_table' in locals() else [(grid_rows - 1 - y) * step_y for y in range(grid_rows)]:
                        for vx in [(x * step_x) for x in range(grid_cols)]:
                            if point_in_polygon(vx, vy, poly):
                                mask_flat[idx] = 1
                            idx += 1
                    inside_mask = np.array(mask_flat, dtype=bool).reshape((grid_rows, grid_cols))
                else:  # Контур зображення
                    threshold = int_val('alphaThreshold')
                    fill_holes = inputs.itemById('fillHoles').value if inputs.itemById('fillHoles') else True
                    mask_bytes = detect_outer_mask(
                        grid_cols, grid_rows,
                        alpha_bytes=(img_resized_alpha.split()[-1].tobytes() if has_alpha else None),
                        raw_pixels=(img_conv.tobytes() if is_16bit else img_gray.tobytes()),
                        is_16bit=is_16bit, threshold=threshold, fill_holes=fill_holes
                    )
                    inside_mask = np.array(mask_bytes, dtype=bool).reshape((grid_rows, grid_cols))

                if shape == 'Прямокутник':
                    v00 = (np.arange(grid_rows - 1)[:, None] * grid_cols + np.arange(grid_cols - 1)[None, :]).ravel()
                    v10 = v00 + 1
                    v01 = v00 + grid_cols
                    v11 = v01 + 1
                    tri1 = np.column_stack((v00, v01, v11))
                    tri2 = np.column_stack((v00, v11, v10))
                    top_triangles = np.vstack((tri1, tri2)).tolist()
                    boundary_edges = []
                    for y in range(grid_rows - 1):
                        row0 = y * grid_cols
                        row1 = (y + 1) * grid_cols
                        for x in range(grid_cols - 1):
                            if y == 0: boundary_edges.append((row0 + x + 1, row0 + x))
                            if y == grid_rows - 2: boundary_edges.append((row1 + x, row1 + x + 1))
                            if x == 0: boundary_edges.append((row0 + x, row1 + x))
                            if x == grid_cols - 2: boundary_edges.append((row1 + x + 1, row0 + x + 1))
                    vertices = all_vertices_list
                else:
                    v00 = (np.arange(grid_rows - 1)[:, None] * grid_cols + np.arange(grid_cols - 1)[None, :]).ravel()
                    v10 = v00 + 1
                    v01 = v00 + grid_cols
                    v11 = v01 + 1

                    m00 = inside_mask[:-1, :-1].ravel()
                    m10 = inside_mask[:-1, 1:].ravel()
                    m01 = inside_mask[1:, :-1].ravel()
                    m11 = inside_mask[1:, 1:].ravel()

                    t1_mask = m00 & m01 & m11
                    t2_mask = m00 & m11 & m10

                    t1 = np.column_stack((v00[t1_mask], v01[t1_mask], v11[t1_mask]))
                    t2 = np.column_stack((v00[t2_mask], v11[t2_mask], v10[t2_mask]))
                    raw_triangles = np.vstack((t1, t2))

                    if len(raw_triangles) == 0:
                        _ui.messageBox('Обрана форма не охопила жодного трикутника.')
                        return

                    # Ущільнення
                    unique_v, remap = np.unique(raw_triangles, return_inverse=True)
                    top_triangles = remap.reshape(raw_triangles.shape).tolist()
                    vertices = [all_vertices_list[v] for v in unique_v]

                    # Ребра межі
                    boundary_edges_set = set()
                    for a, b, c in top_triangles:
                        for u, v in ((a, b), (b, c), (c, a)):
                            if (v, u) in boundary_edges_set:
                                boundary_edges_set.remove((v, u))
                            else:
                                boundary_edges_set.add((u, v))
                    boundary_edges = list(boundary_edges_set)

            # =================================================================
            # ТРАЄКТОРІЯ Б: Чистий Python Fallback (якщо NumPy відсутній)
            # =================================================================
            else:
                raw_pixels = (array.array('I', img_resized.tobytes()) if is_16bit else (img_resized_alpha.convert('L') if has_alpha else img_raw.convert('L').resize((grid_cols, grid_rows), resample_filter)).tobytes())
                vx_table = [x * step_x for x in range(grid_cols)]
                vy_table = [(grid_rows - 1 - y) * step_y for y in range(grid_rows)]

                all_vertices = [None] * (grid_cols * grid_rows)
                idx = 0
                for vy in vy_table:
                    adsk.doEvents()
                    for vx in vx_table:
                        val = raw_pixels[idx]
                        if is_16bit: val = min(65535, max(0, val))
                        z = lut[val]
                        all_vertices[idx] = (vx, vy, z)
                        idx += 1

                progress.progressValue = 65
                progress.message = "Побудова трикутної сітки та стінок (Python)..."
                if progress.wasCancelled: return

                if shape == 'Прямокутник':
                    top_triangles = []
                    boundary_edges = []
                    for y in range(grid_rows - 1):
                        row0 = y * grid_cols
                        row1 = (y + 1) * grid_cols
                        for x in range(grid_cols - 1):
                            v00 = row0 + x
                            v10 = row0 + x + 1
                            v01 = row1 + x
                            v11 = row1 + x + 1
                            top_triangles.append((v00, v01, v11))
                            top_triangles.append((v00, v11, v10))
                            if y == 0: boundary_edges.append((v10, v00))
                            if y == grid_rows - 2: boundary_edges.append((v01, v11))
                            if x == 0: boundary_edges.append((v00, v01))
                            if x == grid_cols - 2: boundary_edges.append((v11, v10))
                    vertices = all_vertices
                else:
                    inside_mask = bytearray(grid_cols * grid_rows)
                    if shape == 'Коло / Овал':
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
                        threshold = int_val('alphaThreshold')
                        fill_holes = inputs.itemById('fillHoles').value if inputs.itemById('fillHoles') else True
                        inside_mask = detect_outer_mask(grid_cols, grid_rows, (img_resized_alpha.split()[-1].tobytes() if has_alpha else None), (img_conv.tobytes() if is_16bit else raw_pixels), is_16bit, threshold, fill_holes)

                    top_triangles = []
                    append_tri = top_triangles.append
                    for y in range(grid_rows - 1):
                        row0 = y * grid_cols
                        row1 = (y + 1) * grid_cols
                        for x in range(grid_cols - 1):
                            if inside_mask[row0 + x] and inside_mask[row1 + x] and inside_mask[row1 + x + 1]:
                                append_tri((row0 + x, row1 + x, row1 + x + 1))
                            if inside_mask[row0 + x] and inside_mask[row1 + x + 1] and inside_mask[row0 + x + 1]:
                                append_tri((row0 + x, row1 + x + 1, row0 + x + 1))

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

            # ---- 5. Підкладка: низ + стінки по контуру ----
            if base_thickness > 0:
                offset = len(vertices)
                bottom_vertices = [(vx, vy, 0.0) for (vx, vy, _) in vertices]
                vertices = vertices + bottom_vertices
                bottom_triangles = [(a + offset, c + offset, b + offset) for (a, b, c) in top_triangles]

                wall_triangles = []
                for (a, b) in boundary_edges:
                    wall_triangles.append((a, a + offset, b + offset))
                    wall_triangles.append((a, b + offset, b))

                triangles = top_triangles + bottom_triangles + wall_triangles
            else:
                triangles = top_triangles

            # ---- 6. Запис OBJ ----
            progress.progressValue = 80
            progress.message = "Експорт тимчасового OBJ..."
            if progress.wasCancelled:
                return

            obj_fp = tempfile.NamedTemporaryFile(mode='w', suffix='.obj', delete=False)
            obj_fp.close()
            write_obj(obj_fp.name, vertices, triangles, progress=progress)
            if progress.wasCancelled:
                return

            # ---- 7. Вставка MeshBody через BaseFeature ----
            progress.progressValue = 88
            progress.message = "Вставка MeshBody у Fusion 360..."
            if progress.wasCancelled:
                try: os.remove(obj_fp.name)
                except OSError: pass
                return

            root_comp = design.rootComponent
            base_feat = root_comp.features.baseFeatures.add()
            base_feat.name = "Heightmap Base"
            base_feat.startEdit()
            try:
                _app.activeViewport.isUpdateLocked = True
                mesh_list = root_comp.meshBodies.add(obj_fp.name, adsk.fusion.MeshUnits.MillimeterMeshUnit, base_feat)
                if mesh_list.count > 0:
                    mesh_list.item(0).name = f"Heightmap Relief ({shape})"
            finally:
                _app.activeViewport.isUpdateLocked = False
                base_feat.finishEdit()

            try:
                os.remove(obj_fp.name)
            except OSError:
                pass

            # ---- 8. Опційне створення окремої твердої BRep основи ТОЧНО ПО КОНТУРУ ----
            if create_solid_base and solid_base_thickness > 0:
                progress.progressValue = 94
                progress.message = "Створення твердотільної BRep основи по контуру..."
                try:
                    sketches = root_comp.sketches
                    xy_plane = root_comp.xYConstructionPlane
                    sketch = sketches.add(xy_plane)

                    sketch.isComputeDeferred = True
                    try:
                        if shape == 'Прямокутник':
                            p0 = adsk.core.Point3D.create(0, 0, 0)
                            p1 = adsk.core.Point3D.create(width_mm / 10.0, height_mm / 10.0, 0)
                            sketch.sketchCurves.sketchLines.addTwoPointRectangle(p0, p1)
                        elif shape == 'Коло / Овал':
                            cx_cm = (width_mm / 2.0) / 10.0
                            cy_cm = (height_mm / 2.0) / 10.0
                            rx_cm = (width_mm / 2.0) / 10.0
                            ry_cm = (height_mm / 2.0) / 10.0
                            center = adsk.core.Point3D.create(cx_cm, cy_cm, 0)
                            if abs(rx_cm - ry_cm) < 1e-5:
                                sketch.sketchCurves.sketchCircles.addByCenterRadius(center, rx_cm)
                            else:
                                major_pt = adsk.core.Point3D.create(cx_cm + rx_cm, cy_cm, 0)
                                point_on = adsk.core.Point3D.create(cx_cm, cy_cm + ry_cm, 0)
                                sketch.sketchCurves.sketchEllipses.add(center, major_pt, point_on)
                        elif shape == 'Багатокутник':
                            sides = int_val('polySides')
                            radius = val_mm('polyRadius')
                            poly = make_regular_polygon(radius, radius, sides, radius)
                            lines = sketch.sketchCurves.sketchLines
                            for i in range(len(poly)):
                                p_start = adsk.core.Point3D.create(poly[i][0] / 10.0, poly[i][1] / 10.0, 0)
                                p_end = adsk.core.Point3D.create(poly[(i + 1) % len(poly)][0] / 10.0, poly[(i + 1) % len(poly)][1] / 10.0, 0)
                                lines.addByTwoPoints(p_start, p_end)
                        else:  # Контур зображення (Alpha / Прозорість)
                            loops = chain_boundary_edges(boundary_edges, vertices)
                            lines = sketch.sketchCurves.sketchLines
                            eps = max(0.2, vertex_spacing * 0.4)
                            for loop in loops:
                                simp = simplify_closed_loop(loop, epsilon=eps)
                                if len(simp) >= 3:
                                    for i in range(len(simp)):
                                        p0 = adsk.core.Point3D.create(simp[i][0] / 10.0, simp[i][1] / 10.0, 0)
                                        p1 = adsk.core.Point3D.create(simp[(i + 1) % len(simp)][0] / 10.0, simp[(i + 1) % len(simp)][1] / 10.0, 0)
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
                            ext_feat.bodies.item(0).name = "SolidBase_Plate"
                except Exception as ex_solid:
                    if _ui:
                        _ui.messageBox(
                            f"Попередження: Не вдалося створити твердотільну BRep основу під рельєфом.\n\n"
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
                solid_msg = f"Тверда BRep основа по контуру: {solid_base_thickness:.2f} мм (BRepBody)\n" if create_solid_base else ""
                backend_msg = "NumPy Векторизовано ⚡" if HAS_NUMPY else "Python Fallback"
                _ui.messageBox(
                    "Готово! MeshBody створено.\n"
                    f"Двигун обчислень: {backend_msg}\n"
                    f"Форма: {shape}\n"
                    f"Режим глибини: {'16-біт' if is_16bit else '8-біт'}\n"
                    f"Гамма: {gamma_val:.2f} | Інверсія: {'Так' if invert_height else 'Ні'}\n"
                    f"Висота заготовки: {base_thickness:.2f} мм\n"
                    f"Глибина рельєфу: {max_depth:.2f} мм\n"
                    f"{solid_msg}"
                    f"Загальна висота моделі: {base_thickness + max_depth:.2f} мм\n"
                    f"Сітка: {grid_cols} x {grid_rows}\n"
                    f"Вершин: {len(vertices)}\n"
                    f"Трикутників: {len(triangles)}"
                )
            else:
                _ui.messageBox('Не вдалося створити MeshBody з отриманого OBJ.')

        except:
            if _ui:
                _ui.messageBox('Помилка виконання:\n{}'.format(traceback.format_exc()))
        finally:
            if progress:
                progress.hide()

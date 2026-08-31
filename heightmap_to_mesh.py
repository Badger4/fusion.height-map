"""
Heightmap -> MeshBody для Fusion 360 (з інтерактивним діалогом)
=================================================================
Що робить:
  1. Даєш вибрати файл зображення через стандартне вікно "Відкрити".
  2. Обираєш форму заготовки: Прямокутник / Коло / Багатокутник,
     і вказуєш її розміри просто в діалозі.
  3. Скрипт будує сітку вершин з жорсткою пропорцією
         Z = (Значення_пікселя / 255) * Максимальна_висота
     і обрізає її по контуру обраної форми.
  4. Опційно додає товщину-підкладку знизу — тоді автоматично
     добудовуються стінки по всьому контуру (працює для будь-якої
     форми, бо межа рахується геометрично, а не вручну по стороні).
  5. Результат вставляється як MeshBody через rootComp.meshBodies.add(...)
     — це підтверджений робочий виклик Fusion API (не BRep, тому
     ядро не "задихається" навіть на великих сітках).

Вимоги:
  - Документ має бути у параметричному режимі
    ("Capture design history" увімкнено).
  - Pillow (PIL) має бути встановлений у Python-середовищі САМЕ Fusion 360,
    не в системному Python:
      Windows:
        "...\\Autodesk\\webdeploy\\production\\<hash>\\Python\\python.exe" -m pip install Pillow
      macOS:
        ".../Autodesk/webdeploy/production/<hash>/Python.framework/Versions/Current/bin/python3" -m pip install Pillow

Запуск: Scripts and Add-Ins -> вибрати файл -> Run.
"""

import adsk.core, adsk.fusion, adsk.cam, traceback
import os
import math
import tempfile

_app = None
_ui = None
handlers = []

CMD_ID = 'heightmapToMeshCmd'
CMD_NAME = 'Heightmap to Mesh'
PANEL_ID = 'SolidCreatePanel'

MAX_VERTICES_WARNING = 400000  # поріг попередження про розмір сітки


# =====================================================================
# Геометричні допоміжні функції
# =====================================================================

def make_regular_polygon(cx, cy, sides, radius):
    """Вершини правильного багатокутника, вписаного в коло радіусом radius."""
    verts = []
    for i in range(sides):
        angle = -math.pi / 2.0 + i * (2.0 * math.pi / sides)
        verts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return verts


def point_in_convex_polygon(px, py, poly):
    """Перевірка належності точки опуклому багатокутнику (усі кути в один бік)."""
    n = len(poly)
    sign = None
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        if abs(cross) < 1e-9:
            continue
        s = cross > 0
        if sign is None:
            sign = s
        elif s != sign:
            return False
    return True


def write_obj(path, vertices, triangles):
    """Мінімальний ASCII OBJ: вершини 'v x y z' і грані 'f i j k' (1-indexed)."""
    with open(path, 'w') as f:
        for (x, y, z) in vertices:
            f.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
        for (i1, i2, i3) in triangles:
            f.write(f"f {i1 + 1} {i2 + 1} {i3 + 1}\n")


# =====================================================================
# add-in lifecycle
# =====================================================================

def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

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
        adsk.autoTerminate(False)  # тримаємо скрипт живим, поки діалог відкритий
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

            # ---- Зображення ----
            img_input = inputs.addStringValueInput('imagePath', 'Файл зображення', '')
            img_input.isReadOnly = True
            inputs.addBoolValueInput('browseBtn', 'Обрати зображення...', False, '', False)

            # ---- Форма заготовки ----
            shape_dd = inputs.addDropDownCommandInput(
                'shapeType', 'Форма заготовки', adsk.core.DropDownStyles.TextListDropDownStyle
            )
            shape_dd.listItems.add('Прямокутник', True, '')
            shape_dd.listItems.add('Коло', False, '')
            shape_dd.listItems.add('Багатокутник', False, '')

            mm = 'mm'

            # Прямокутник
            rect_w = inputs.addValueInput('rectWidth', 'Ширина (X)', mm, adsk.core.ValueInput.createByString('100 mm'))
            rect_l = inputs.addValueInput('rectLength', 'Довжина (Y)', mm, adsk.core.ValueInput.createByString('100 mm'))

            # Коло
            circle_d = inputs.addValueInput('circleDia', 'Діаметр', mm, adsk.core.ValueInput.createByString('100 mm'))
            circle_d.isVisible = False

            # Багатокутник
            poly_sides = inputs.addIntegerSpinnerCommandInput('polySides', 'Кількість граней', 3, 24, 1, 6)
            poly_sides.isVisible = False
            poly_r = inputs.addValueInput('polyRadius', 'Радіус (до вершини)', mm, adsk.core.ValueInput.createByString('50 mm'))
            poly_r.isVisible = False

            inputs.addSeparatorCommandInput('sep1')

            # ---- Параметри рельєфу ----
            inputs.addValueInput('maxDepth', 'Глибина рельєфу (білий піксель = 255)', mm, adsk.core.ValueInput.createByString('2 mm'))
            inputs.addValueInput('baseThickness', 'Висота заготовки (товщина під рельєфом, 0 = без дна)', mm, adsk.core.ValueInput.createByString('5 mm'))
            inputs.addValueInput('vertexSpacing', 'Крок сітки (деталізація)', mm, adsk.core.ValueInput.createByString('0.5 mm'))
            inputs.addIntegerSpinnerCommandInput('smoothPasses', 'Проходи згладжування', 0, 10, 1, 1)

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


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            changed = args.input
            inputs = args.inputs

            if changed.id == 'browseBtn':
                bool_input = adsk.core.BoolValueCommandInput.cast(changed)
                if bool_input.value:
                    file_dlg = _ui.createFileDialog()
                    file_dlg.title = 'Обрати карту висот'
                    file_dlg.filter = 'Зображення (*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff)'
                    file_dlg.isMultiSelectEnabled = False
                    result = file_dlg.showOpen()
                    if result == adsk.core.DialogResults.DialogOK:
                        path_input = adsk.core.StringValueCommandInput.cast(inputs.itemById('imagePath'))
                        path_input.value = file_dlg.filename
                    bool_input.value = False  # скидаємо кнопку назад

            if changed.id == 'shapeType':
                dd = adsk.core.DropDownCommandInput.cast(changed)
                shape = dd.selectedItem.name
                is_rect = (shape == 'Прямокутник')
                is_circle = (shape == 'Коло')
                is_poly = (shape == 'Багатокутник')

                inputs.itemById('rectWidth').isVisible = is_rect
                inputs.itemById('rectLength').isVisible = is_rect
                inputs.itemById('circleDia').isVisible = is_circle
                inputs.itemById('polySides').isVisible = is_poly
                inputs.itemById('polyRadius').isVisible = is_poly
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
        try:
            inputs = args.command.commandInputs

            # ---- Зображення ----
            image_path = adsk.core.StringValueCommandInput.cast(inputs.itemById('imagePath')).value
            if not image_path or not os.path.isfile(image_path):
                _ui.messageBox('Файл зображення не обрано або не знайдено.')
                return

            try:
                from PIL import Image
            except ImportError:
                _ui.messageBox(
                    "Модуль Pillow (PIL) не знайдено у Python-середовищі Fusion 360.\n\n"
                    "Встанови його через python.exe всередині теки Fusion 360, "
                    "а не через системний Python."
                )
                return

            # ---- Перевірка параметричного режиму (потрібен для BaseFeature) ----
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
                # ValueCommandInput.value повертає внутрішні одиниці Fusion (см)
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

            # ---- Габарити та функція-маска для обраної форми ----
            if shape == 'Прямокутник':
                width_mm = val_mm('rectWidth')
                height_mm = val_mm('rectLength')
                mask_fn = lambda x, y: True
            elif shape == 'Коло':
                dia = val_mm('circleDia')
                width_mm = height_mm = dia
                cx, cy = dia / 2.0, dia / 2.0
                r = dia / 2.0
                mask_fn = lambda x, y, cx=cx, cy=cy, r=r: (x - cx) ** 2 + (y - cy) ** 2 <= r ** 2 + 1e-6
            else:  # Багатокутник
                sides = int_val('polySides')
                radius = val_mm('polyRadius')
                width_mm = height_mm = radius * 2.0
                cx, cy = radius, radius
                poly = make_regular_polygon(cx, cy, sides, radius)
                mask_fn = lambda x, y, poly=poly: point_in_convex_polygon(x, y, poly)

            if width_mm <= 0 or height_mm <= 0:
                _ui.messageBox('Розміри заготовки мають бути більше нуля.')
                return

            grid_cols = max(2, int(round(width_mm / vertex_spacing)) + 1)
            grid_rows = max(2, int(round(height_mm / vertex_spacing)) + 1)

            if grid_cols * grid_rows > MAX_VERTICES_WARNING:
                res = _ui.messageBox(
                    f'Сітка буде {grid_cols}x{grid_rows} = {grid_cols * grid_rows} вершин.\n'
                    f'Це може бути повільно.\n\n'
                    f'Збільш "Крок сітки", щоб пришвидшити.\n\nПродовжити попри це?',
                    'Попередження про розмір',
                    adsk.core.MessageBoxButtonTypes.YesNoButtonType,
                    adsk.core.MessageBoxIconTypes.WarningIconType
                )
                if res != adsk.core.DialogResults.DialogYes:
                    return

            # ---- Завантаження і підготовка зображення ----
            img = Image.open(image_path).convert('L').resize((grid_cols, grid_rows), Image.LANCZOS)
            pixels = list(img.getdata())

            def get_px(x, y):
                x = min(max(x, 0), grid_cols - 1)
                y = min(max(y, 0), grid_rows - 1)
                return pixels[y * grid_cols + x]

            # ---- Згладжування (усереднення 3x3) ----
            for _ in range(smooth_passes):
                smoothed = [0.0] * len(pixels)
                for y in range(grid_rows):
                    for x in range(grid_cols):
                        total = 0
                        count = 0
                        for dy in (-1, 0, 1):
                            for dx in (-1, 0, 1):
                                total += get_px(x + dx, y + dy)
                                count += 1
                        smoothed[y * grid_cols + x] = total / count
                pixels = smoothed

            # ---- Пряма пропорція: Z = (Pixel/255) * MAX_DEPTH ----
            step_x = width_mm / (grid_cols - 1)
            step_y = height_mm / (grid_rows - 1)

            def idx(x, y):
                return y * grid_cols + x

            all_vertices = [None] * (grid_cols * grid_rows)
            for y in range(grid_rows):
                for x in range(grid_cols):
                    value = pixels[idx(x, y)]
                    z = (value / 255.0) * max_depth + base_thickness
                    vx = x * step_x
                    vy = (grid_rows - 1 - y) * step_y  # інвертуємо Y, щоб не було дзеркала
                    all_vertices[idx(x, y)] = (vx, vy, z)

            # ---- Трикутники, обрізані по масці форми ----
            all_triangles = []
            for y in range(grid_rows - 1):
                for x in range(grid_cols - 1):
                    v00 = idx(x, y); v10 = idx(x + 1, y)
                    v01 = idx(x, y + 1); v11 = idx(x + 1, y + 1)
                    corners = [all_vertices[v00], all_vertices[v10], all_vertices[v01], all_vertices[v11]]
                    if all(mask_fn(vx, vy) for (vx, vy, _) in corners):
                        all_triangles.append((v00, v10, v11))
                        all_triangles.append((v00, v11, v01))

            if not all_triangles:
                _ui.messageBox('Форма не охопила жодної комірки сітки. Перевір розміри.')
                return

            # ---- Ущільнення: прибираємо вершини поза контуром ----
            used = sorted(set(i for tri in all_triangles for i in tri))
            remap = {old: new for new, old in enumerate(used)}
            vertices = [all_vertices[i] for i in used]
            triangles = [(remap[a], remap[b], remap[c]) for (a, b, c) in all_triangles]

            # ---- Підкладка: низ + стінки по фактичному контуру (працює для будь-якої форми) ----
            if base_thickness > 0:
                top_triangles = list(triangles)
                offset = len(vertices)
                vertices += [(vx, vy, 0.0) for (vx, vy, _) in vertices[:offset]]

                bottom_triangles = [(a + offset, c + offset, b + offset) for (a, b, c) in top_triangles]

                # Межа = ребра, що належать лише одному трикутнику верхньої поверхні
                directed_edges = set()
                for (a, b, c) in top_triangles:
                    directed_edges.add((a, b))
                    directed_edges.add((b, c))
                    directed_edges.add((c, a))
                boundary_edges = [(a, b) for (a, b) in directed_edges if (b, a) not in directed_edges]

                wall_triangles = []
                for (a, b) in boundary_edges:
                    wall_triangles.append((a, b, b + offset))
                    wall_triangles.append((a, b + offset, a + offset))

                triangles = top_triangles + bottom_triangles + wall_triangles

            # ---- Запис OBJ ----
            obj_fp = tempfile.NamedTemporaryFile(mode='w', suffix='.obj', delete=False)
            obj_fp.close()
            write_obj(obj_fp.name, vertices, triangles)

            # ---- Вставка MeshBody через BaseFeature ----
            root_comp = design.rootComponent
            base_feat = root_comp.features.baseFeatures.add()
            base_feat.startEdit()
            try:
                mesh_list = root_comp.meshBodies.add(obj_fp.name, adsk.fusion.MeshUnits.MillimeterMeshUnit, base_feat)
            finally:
                base_feat.finishEdit()

            try:
                os.remove(obj_fp.name)
            except OSError:
                pass

            if mesh_list.count > 0:
                _app.activeViewport.fit()
                _ui.messageBox(
                    "Готово! MeshBody створено.\n"
                    f"Форма: {shape}\n"
                    f"Висота заготовки: {base_thickness:.2f} мм\n"
                    f"Глибина рельєфу: {max_depth:.2f} мм\n"
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

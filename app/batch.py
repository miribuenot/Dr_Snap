import csv
import os
import shutil
import uuid
import re
from datetime import datetime
from zipfile import ZipFile
from .models import BatchCSV

# ==============================================================================
# FUNCIONES AUXILIARES (TRADUCCIÓN)
# ==============================================================================

def skills_translation(request) -> dict:
    """
    Crea un diccionario para traducir las habilidades en el resumen final.
    """
    lang = request.LANGUAGE_CODE
    if lang == "es":
        dic = {'Pensamiento lógico':'Logic', 'Paralelismo':'Parallelism', 'Representación de la información':'DataRepresentation', 'Sincronización':'Synchronization', 'Interactividad con el usuario':'UserInteractivity', 'Control de flujo':'FlowControl', 'Abstracción':'Abstraction', 'Operadores matemáticos':'MathOperators', 'Operadores de movimiento': 'MotionOperators'}
    else: # Default EN
        dic = {u'Logic': 'Logic', u'Parallelism':'Parallelism', u'Data representation':'DataRepresentation', u'Synchronization':'Synchronization', u'User interactivity':'UserInteractivity', u'Flow control':'FlowControl', u'Abstraction':'Abstraction', u'Math operators':'MathOperators', u'Motion operators': 'MotionOperators'}
    return dic

# ==============================================================================
# FUNCIONES GENERADORAS DE CSV INDIVIDUALES
# ==============================================================================

def create_csv_main(request, d: dict, folder_path: str) -> str:
    csv_name = "main.csv"
    csv_filepath = os.path.join(folder_path, csv_name)
    
    headers = [
        'url', 'filename', 'points', 
        'Abstraction', 'Parallelism', 'Logic', 'Synchronization',
        'Flow control', 'User interactivity', 'Data representation',
        'Math operators', 'Motion operators', 'DuplicateScripts',
        'DeadCode', 'SpriteNaming', 'BackdropNaming', 
        'Error', 'dashboard_mode'
    ]
    vanilla_headers = ['Van points','Van Abstraction','Van Parallelism', 'Van Logic', 'Van Synchronization', 'Van Flow control', 'Van User interactivity', 'Van Data representation']
    global_headers = headers + vanilla_headers + ['tot_blocks']

    keys_map = {
        'Abstraction': 'Abstraction', 'Parallelism': 'Parallelization', 'Logic': 'Logic',
        'Synchronization': 'Synchronization', 'Flow control': 'FlowControl',
        'User interactivity': 'UserInteractivity', 'Data representation': 'DataRepresentation',
        'Math operators': 'MathOperators', 'Motion operators': 'MotionOperators'
    }

    with open(csv_filepath, 'w', newline='') as csv_file:
        writer_csv = csv.DictWriter(csv_file, fieldnames=global_headers)
        writer_csv.writeheader()

        for project in d.values():
            row = {}
            # Datos básicos
            row['url'] = project.get('url', 'Upload')
            row['filename'] = re.sub(r"[\;\"\,\n\r]", "", str(project.get('filename', '')))
            row['Error'] = project.get('Error', 'None')
            
            # Bloques
            try: row['tot_blocks'] = project['block_sprite_usage']['result']['total_blocks']
            except: row['tot_blocks'] = 'N/A'

            # Puntos Extended (Buscamos en 'extended', 'mastery' o raíz)
            mastery = project.get('extended') or project.get('mastery') or project
            pts = mastery.get('total_points') or mastery.get('points') or [0]
            row['points'] = pts[0] if isinstance(pts, list) else pts

            for csv_k, dict_k in keys_map.items():
                val = mastery.get(dict_k, [0])
                row[csv_k] = f"{val[0]}/{val[1]}" if isinstance(val, list) and len(val)>1 else val

            # Puntos Vanilla
            vanilla = project.get('vanilla') or project.get('mastery_vanilla') or {}
            pts_van = vanilla.get('total_points') or vanilla.get('points') or [0]
            row['Van points'] = pts_van[0] if isinstance(pts_van, list) else pts_van

            for csv_k, dict_k in keys_map.items():
                if csv_k not in ['Math operators', 'Motion operators']:
                    val = vanilla.get(dict_k, [0])
                    row[f"Van {csv_k}"] = f"{val[0]}/{val[1]}" if isinstance(val, list) and len(val)>1 else val

            # Bad Smells (Con protección .get segura)
            row['DuplicateScripts'] = project.get('duplicateScript', {}).get('number', 0)
            row['DeadCode'] = project.get('deadCode', {}).get('number', 0)
            row['SpriteNaming'] = project.get('spriteNaming', {}).get('number', 0)
            row['BackdropNaming'] = project.get('backdropNaming', {}).get('number', 0)

            writer_csv.writerow(row)
    return csv_filepath

def create_csv_dups(d: dict, folder_path: str):
    csv_name = "duplicateScript.csv"
    csv_filepath = os.path.join(folder_path, csv_name)
    headers = ['url', 'filename', 'number']

    max_dup_scripts = 0
    for project in d.values():
        dups = project.get('duplicateScript', {}).get('csv_format', [])
        count = len(dups) if dups else 0
        if count > max_dup_scripts: 
            max_dup_scripts = count
    
    for i in range(1, max_dup_scripts + 1):
        headers.append(f'duplicateScript_{i}')

    with open(csv_filepath, 'w', newline='', encoding='utf-8') as csv_file:
        writer_csv = csv.DictWriter(csv_file, fieldnames=headers)
        writer_csv.writeheader()

        for project in d.values():
            row = {
                'url': project.get('url', ''),
                'filename': project.get('filename', ''),
                'number': project.get('duplicateScript', {}).get('number', 0)
            }
            dups = project.get('duplicateScript', {}).get('csv_format', [])
            idx = 1
            if dups:
                for script in dups:
                    row[f'duplicateScript_{idx}'] = script
                    idx += 1
            writer_csv.writerow(row)

def create_csv_sprites(d: dict, folder_path: str):
    csv_name = "spriteNaming.csv"
    csv_filepath = os.path.join(folder_path, csv_name)
    headers = ['url', 'filename', 'number']

    max_sprites = 0
    for p in d.values():
        sprites = p.get('spriteNaming', {}).get('sprite', [])
        if len(sprites) > max_sprites: max_sprites = len(sprites)
    
    headers.extend(f'spriteNaming{i}' for i in range(1, max_sprites+1))
    
    with open(csv_filepath, 'w', newline='') as csv_file:
        writer_csv = csv.DictWriter(csv_file, fieldnames=headers)
        writer_csv.writeheader()

        for project in d.values():
            row = {
                'url': project.get('url', ''),
                'filename': project.get('filename', ''),
                'number': project.get('spriteNaming', {}).get('number', 0)
            }
            sprites = project.get('spriteNaming', {}).get('sprite', [])
            for i, name in enumerate(sprites, 1):
                row[f'spriteNaming{i}'] = name
            writer_csv.writerow(row)

def create_csv_backdrops(d: dict, folder_path: str):
    csv_name = "backdropNaming.csv"
    csv_filepath = os.path.join(folder_path, csv_name)
    headers = ['url', 'filename', 'number']

    max_b = 0
    for p in d.values():
        b = p.get('backdropNaming', {}).get('backdrop', [])
        if len(b) > max_b: max_b = len(b)
    
    headers.extend(f'backdropNaming{i}' for i in range(1, max_b+1))
    
    with open(csv_filepath, 'w', newline='') as csv_file:
        writer_csv = csv.DictWriter(csv_file, fieldnames=headers)
        writer_csv.writeheader()

        for project in d.values():
            row = {
                'url': project.get('url', ''),
                'filename': project.get('filename', ''),
                'number': project.get('backdropNaming', {}).get('number', 0)
            }
            backdrops = project.get('backdropNaming', {}).get('backdrop', [])
            for i, name in enumerate(backdrops, 1):
                row[f'backdropNaming{i}'] = name
            writer_csv.writerow(row)

def create_csv_deadcode(d: dict, folder_path: str):
    csv_name = "deadCode.csv"
    csv_filepath = os.path.join(folder_path, csv_name)
    headers = ['url', 'filename', 'number', 'sprite']
    
    # Calcular columnas maximas
    max_cols = 0
    for p in d.values():
        dc_data = p.get('deadCode', {})
        for k, v in dc_data.items():
            if k not in ['number', 'deadCode'] and isinstance(v, list):
                if len(v) > max_cols: max_cols = len(v)
    
    headers.extend(f'deadCode{i}' for i in range(1, max_cols+1))
        
    with open(csv_filepath, 'w', newline='') as csv_file:
        writer_csv = csv.DictWriter(csv_file, fieldnames=headers)
        writer_csv.writeheader()

        for project in d.values():
            dc_data = project.get('deadCode', {})
            # Iteramos por sprites dentro de deadCode
            for sprite_name, blocks in dc_data.items():
                if sprite_name not in ['number', 'deadCode']:
                    row = {
                        'url': project.get('url', ''),
                        'filename': project.get('filename', ''),
                        'number': dc_data.get('number', 0),
                        'sprite': sprite_name
                    }
                    for i, block in enumerate(blocks, 1):
                        row[f'deadCode{i}'] = block
                    writer_csv.writerow(row)

# ==============================================================================
# FUNCIONES DE RESUMEN Y BASE DE DATOS
# ==============================================================================

def create_summary(request, d: dict) -> dict:
    summary = {}
    skills = ['Abstraction', 'Parallelization', 'Logic', 'Synchronization', 'FlowControl', 
              'UserInteractivity', 'DataRepresentation', 'MathOperators', 'MotionOperators']
    
    for s in skills: summary[s] = 0
    summary['Points'] = 0
    summary['num_projects'] = len(d)

    total_max = 21
    
    for p in d.values():
        m = p.get('extended') or p.get('mastery') or p
        pts = m.get('total_points') or m.get('points') or [0]
        val_points = pts[0] if isinstance(pts, list) else pts
        summary['Points'] += val_points

        for s in skills:
            val = m.get(s, [0])
            score = val[0] if isinstance(val, list) else val
            summary[s] += score
            
            if isinstance(val, list) and len(val) > 1:
                 pass 

    n = len(d) if len(d) > 0 else 1
    summary['Points'] = [round(summary['Points']/n, 2), total_max]
    for s in skills:
        summary[s] = [round(summary[s]/n, 2), 3]

    avg = summary['Points'][0]
    if avg >= 15: summary['Mastery'] = 'Master'
    elif avg > 7: summary['Mastery'] = 'Developing'
    else: summary['Mastery'] = 'Basic'

    return summary

def create_obj(data: dict, csv_filepath: str) -> uuid.UUID:
    cs_data = BatchCSV.objects.create(
        filepath= csv_filepath,
        num_projects=data['num_projects'],
        points=data['Points'][0],
        max_points=data['Points'][1],
        logic=data['Logic'][0],
        max_logic=data['Logic'][1],
        parallelization=data['Parallelization'][0],
        max_parallelization=data['Parallelization'][1],
        data=data['DataRepresentation'][0],
        max_data=data['DataRepresentation'][1],
        synchronization=data['Synchronization'][0],
        max_synchronization=data['Synchronization'][1],
        userInteractivity=data['UserInteractivity'][0],
        max_userInteractivity=data['UserInteractivity'][1],
        flowControl=data['FlowControl'][0],
        max_flowControl=data['FlowControl'][1],
        abstraction=data['Abstraction'][0],
        max_abstraction=data['Abstraction'][1],
        math_operators=data['MathOperators'][0],
        max_math_operators=data['MathOperators'][1],
        motion_operators=data['MotionOperators'][0],
        max_motion_operators=data['MotionOperators'][1],
        mastery=data['Mastery']
    )
    return cs_data.id

def zip_folder(folder_path: str):
    zip_path = folder_path + '.zip'
    with ZipFile(zip_path, 'w') as zipObj:
        for folderName, subfolders, filenames in os.walk(folder_path):
            for filename in filenames:
                filePath = os.path.join(folderName, filename)
                zipObj.write(filePath, os.path.basename(filePath))
    shutil.rmtree(folder_path)
    return zip_path

# ==============================================================================
# FUNCIÓN PRINCIPAL DE ENTRADA
# ==============================================================================

def create_csv(request, d: dict) -> uuid.UUID:
    now = datetime.now()
    folder_name = str(uuid.uuid4()) + '_' + now.strftime("%Y%m%d%H%M%S")
    base_dir = os.getcwd()
    folder_path = os.path.join(base_dir, 'csvs', 'Dr.Scratch', folder_name)
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
    
    create_csv_main(request, d, folder_path)
    create_csv_dups(d, folder_path)
    create_csv_sprites(d, folder_path)
    create_csv_backdrops(d, folder_path)
    create_csv_deadcode(d, folder_path)
    
    summary = create_summary(request, d) 
    
    csv_filepath = zip_folder(folder_path)
    id = create_obj(summary, csv_filepath)
    
    return id
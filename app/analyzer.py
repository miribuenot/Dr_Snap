import json
import os
import shutil
import traceback
import uuid
import logging
import requests
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from urllib.parse import quote, urlparse, parse_qs
from zipfile import BadZipfile, ZipFile

from django.http import HttpResponseRedirect
from app.exception import DrScratchException

# Imports de Hairball
from app.hairball3.backdropNaming import BackdropNaming
from app.hairball3.deadCode import DeadCode
from app.hairball3.mastery import Mastery
from app.hairball3.spriteNaming import SpriteNaming
from app.hairball3.scratchGolfing import ScratchGolfing
from app.hairball3.block_sprite_usage import Block_Sprite_Usage
from app.models import Coder, File, Organization
from app.scratchclient import ScratchSession
from app.recomender import RecomenderSystem
import app.consts_drscratch as consts
from lxml import etree

# Intentamos importar DuplicateScripts de forma segura
try:
    from app.hairball3.duplicateScripts import DuplicateScripts
except ImportError:
    DuplicateScripts = None

logger = logging.getLogger(__name__)

# ==============================================================================
# 1. FUNCIONES DE TRADUCCIÓN Y FORMATO (Dashboard Web)
# ==============================================================================

def translate(request, d, filename, vanilla=False):
    """
    Traduce la salida de Hairball para que el HTML la entienda.
    """
    lang = request.LANGUAGE_CODE
    translations = {
        "es": {'Abstraction': 'Abstracción', 'Parallelization': 'Paralelismo', 'Logic': 'Pensamiento lógico', 'Synchronization': 'Sincronización', 'FlowControl': 'Control de flujo', 'UserInteractivity': 'Interactividad con el usuario', 'DataRepresentation': 'Representación de la información', 'MathOperators': 'Operadores matemáticos', 'MotionOperators': 'Operadores de movimiento'},
        "en": {'Abstraction': 'Abstraction', 'Parallelization': 'Parallelism', 'Logic': 'Logic', 'Synchronization': 'Synchronization', 'FlowControl': 'Flow control', 'UserInteractivity': 'User interactivity', 'DataRepresentation': 'Data representation', 'MathOperators': 'Math operators', 'MotionOperators': 'Motion operators'},
    }
    # Fallback
    t_map = translations.get(lang, translations['en'])
    
    result = {}
    keys = ['Abstraction', 'Parallelization', 'Logic', 'Synchronization', 'FlowControl', 'UserInteractivity', 'DataRepresentation']
    if not vanilla:
        keys.extend(['MathOperators', 'MotionOperators'])

    for key in keys:
        # El HTML espera la lista [puntos, nombre_clave]
        raw_val = d.get(key, [0, 0])
        # Aseguramos que sea una lista si viene como número suelto
        if not isinstance(raw_val, list):
            raw_val = [raw_val, 3] 

        result[t_map.get(key, key)] = [raw_val, key]

    try:
        filename.language = lang if lang in translations else "en"
        filename.save()
    except: pass
    
    return result

def proc_mastery(request, dict_mastery, file_obj):
    """
    Prepara el diccionario 'mastery' que usan las plantillas HTML.
    """
    dic = {}
    mode = request.POST.get('dashboard_mode', 'Default')
    non_personalized = ['Default', 'Comparison', 'Recommender']
    
    # Asegurar que existen los diccionarios base
    raw_extended = dict_mastery.get('extended', {})
    raw_vanilla = dict_mastery.get('vanilla', {})

    if mode in non_personalized:
        # Guardar en BD (usando datos crudos)
        set_file_obj(request, file_obj, raw_extended)
        if raw_vanilla:
            set_file_obj(request, file_obj, raw_vanilla, 'Vanilla')
        
        # Traducir para la vista (HTML)
        d_extended_translated = translate(request, raw_extended, file_obj)
        d_vanilla_translated = translate(request, raw_vanilla, file_obj, vanilla=True)
        
        dic = {"mastery": d_extended_translated, "mastery_vanilla": d_vanilla_translated}
        dic["mastery"]["competence"] = raw_extended.get("competence", "Unknown")
        dic["mastery"]["points"] = raw_extended.get("total_points", [0])
        dic["mastery_vanilla"]["competence"] = raw_vanilla.get("competence", "Unknown")
        dic["mastery_vanilla"]["points"] = raw_vanilla.get("total_points", [0])     
        
    elif mode == 'Personalized':
        raw_personal = dict_mastery.get('personalized', {})
        set_file_obj(request, file_obj, raw_personal)
        d_personal_translated = translate(request, raw_personal, file_obj)
        dic = {"mastery": d_personal_translated}
        dic["mastery"]["competence"] = raw_personal.get("competence", "Unknown")
        dic["mastery"]["points"] = raw_personal.get("total_points", [0])
    
    return dic

def set_file_obj(request, file_obj, dict_data, mode=None):
    try:
        # --- CORRECCIÓN DE SEGURIDAD PARA PUNTOS NULOS ---
        points = dict_data.get("total_points", [0])
        if points is None: points = [0]
        score_val = points[0] if isinstance(points, list) else points
        
        file_obj.score = score_val
        file_obj.competence = dict_data.get("competence", "Unknown")
        
        skills_map = {
            'abstraction': 'Abstraction', 'parallelization': 'Parallelization', 
            'logic': 'Logic', 'synchronization': 'Synchronization', 
            'flow_control': 'FlowControl', 'userInteractivity': 'UserInteractivity', 
            'dataRepresentation': 'DataRepresentation',
            'mathOperators': 'MathOperators', 'motionOperators': 'MotionOperators'
        }
        
        for db_field, key in skills_map.items():
            val = dict_data.get(key, [0])
            if val is None: val = [0] # Protección extra
            score = val[0] if isinstance(val, list) else val
            
            if hasattr(file_obj, db_field):
                setattr(file_obj, db_field, score)
                
        file_obj.save()
    except Exception as e:
        logger.error(f"Error saving file object stats: {e}")

# ==============================================================================
# 2. FUNCIONES DE BAD SMELLS (PROC_*)
# ==============================================================================

def proc_duplicate_script(dict_result, file_obj) -> dict:
    dict_ds = {}
    try:
        dict_ds["duplicateScript"] = {
            "number": dict_result['result']['total_duplicate_scripts'],
            "scripts": dict_result['result']['list_duplicate_scripts'],
            "csv_format": dict_result['result']['list_csv']
        }
        file_obj.duplicateScript = dict_result['result']['total_duplicate_scripts']
        file_obj.save()
    except Exception:
        dict_ds["duplicateScript"] = {"number": 0, "csv_format": []}
    return dict_ds

def proc_dead_code(dict_dead_code, filename):
    dict_dc = {"deadCode": {"number": 0}}
    try:
        dict_dc["deadCode"]["number"] = dict_dead_code['result']['total_dead_code_scripts']
        for dict_sprite_dead_code_blocks in dict_dead_code['result']['list_dead_code_scripts']:
            for sprite_name, list_blocks in dict_sprite_dead_code_blocks.items():
                dict_dc["deadCode"][sprite_name] = list_blocks
        filename.deadCode = dict_dead_code['result']['total_dead_code_scripts']
        filename.save()
    except Exception: pass
    return dict_dc

def proc_sprite_naming(lines, file_obj):
    dic = {'spriteNaming': {'number': 0, 'sprite': []}}
    try:
        lLines = lines.split('\n')
        number = lLines[0].split(' ')[0]
        lObjects = lLines[1:]
        lfinal = lObjects[:-1]
        dic['spriteNaming'] = {'number': int(number), 'sprite': lfinal}
        file_obj.spriteNaming = number
        file_obj.save()
    except Exception: pass
    return dic

def proc_backdrop_naming(lines, file_obj):
    dic = {'backdropNaming': {'number': 0, 'backdrop': []}}
    try:
        lLines = lines.split('\n')
        number = lLines[0].split(' ')[0]
        lObjects = lLines[1:]
        lfinal = lObjects[:-1]
        dic['backdropNaming'] = {'number': int(number), 'backdrop': lfinal}
        file_obj.backdropNaming = number
        file_obj.save()
    except Exception: pass
    return dic

def proc_block_sprite_usage(result_block_sprite_usage, filename, json_project=None):
    try:
        current_total = result_block_sprite_usage.get('result', {}).get('total_blocks', 0)
        # Recálculo manual si es 0 (Fix para XML antiguo)
        if current_total == 0 and json_project:
            manual_count = 0
            for key, val in json_project.items():
                if isinstance(val, dict) and 'blocks' in val:
                    manual_count += len(val['blocks'])
            if 'result' not in result_block_sprite_usage:
                result_block_sprite_usage['result'] = {}
            result_block_sprite_usage['result']['total_blocks'] = manual_count
    except Exception: pass
    return {"block_sprite_usage": result_block_sprite_usage}

def proc_recomender(dict_recom):
    recomender = {'recomenderSystem': {'message': "Congrat's you don't have any bad smell at the moment."}}
    if dict_recom.get("deadCode"):
        recomender = {'recomenderSystem': dict_recom["deadCode"]}
        RecomenderSystem.curr_type = dict_recom["deadCode"]['type']
        return recomender
    if dict_recom.get("spriteNaming"):
        recomender = {'recomenderSystem': dict_recom["spriteNaming"]}
        RecomenderSystem.curr_type = dict_recom["spriteNaming"]['type']
        return recomender
    if dict_recom.get("backdropNaming"):
        recomender = {'recomenderSystem': dict_recom["backdropNaming"]}
        RecomenderSystem.curr_type = dict_recom["backdropNaming"]['type']
        return recomender
    return recomender

# ==============================================================================
# 3. CORE DE ANÁLISIS (EL MOTOR)
# ==============================================================================

def analyze_project(request, info_project, skill_points: dict, filename_obj, file_obj):
    dict_analysis = {}
    dashboard = request.POST.get('dashboard_mode', 'Default')
    curr_type = request.POST.get('curr_type', '')

    try:
        # 1. Cargar Proyecto
        if info_project.get("projectname"):
            scratch_project_inf = get_snap_project_xml(info_project['username'], info_project['projectname'])
        else:
            scratch_project_inf = load_json_project(filename_obj)
        
        # Parseo XML Seguro
        json_snap_project = split_xml(request, scratch_project_inf)
        path_projectsb3 = info_project.get("projectname", "upload")

        # 2. Análisis por módulos
        
        # A) MASTERY (Puntos)
        try:
            # Obtenemos datos CRUDOS (English)
            raw_mastery = Mastery(path_projectsb3, json_snap_project, skill_points, dashboard).finalize()
            
            # --- FUSIÓN CRUCIAL ---
            # 1. Guardamos datos crudos para Batch/CSV
            dict_analysis['extended'] = raw_mastery.get('extended')
            dict_analysis['vanilla'] = raw_mastery.get('vanilla')
            dict_analysis['total_points'] = raw_mastery.get('total_points') 
            
            # 2. Procesamos para Dashboard Web (Traducido)
            processed_mastery = proc_mastery(request, raw_mastery, file_obj)
            dict_analysis.update(processed_mastery)
            
        except Exception as e:
            logger.error(f"Mastery Error: {e}")
            dict_analysis['Error'] = 'mastery_error' 

        # B) DEAD CODE
        try:
            dict_dead_code = DeadCode(path_projectsb3, json_snap_project).finalize()
            dict_analysis.update(proc_dead_code(dict_dead_code, file_obj))
        except Exception: pass

        # C) NAMING
        try:
            result_sprite_naming = SpriteNaming(path_projectsb3, json_snap_project).finalize()
            dict_analysis.update(proc_sprite_naming(result_sprite_naming, file_obj))
        except Exception: pass
        
        try:
            result_backdrop_naming = BackdropNaming(path_projectsb3, json_snap_project).finalize()
            dict_analysis.update(proc_backdrop_naming(result_backdrop_naming, file_obj))
        except Exception: pass

        # D) BLOCK USAGE
        try:
            result_block_sprite_usage = Block_Sprite_Usage(path_projectsb3, json_snap_project).finalize()
            dict_analysis.update(proc_block_sprite_usage(result_block_sprite_usage, file_obj, json_snap_project))
        except Exception: pass

        # E) DUPLICATE SCRIPTS
        try:
            if DuplicateScripts:
                result_duplicate_script = DuplicateScripts(path_projectsb3, json_snap_project).finalize()
                dict_analysis.update(proc_duplicate_script(result_duplicate_script, file_obj))
            else:
                dict_analysis['duplicateScript'] = {'number': 0}
        except Exception:
            dict_analysis['duplicateScript'] = {'number': 0}

        # F) RECOMMENDER
        if dashboard == 'Recommender':
            try:
                dict_recom = {}
                recomender = RecomenderSystem(curr_type)
                if 'deadCode' in dict_analysis:
                    dict_recom["deadCode"] = recomender.recomender_deadcode(dict_dead_code)
                if 'spriteNaming' in dict_analysis:
                    dict_recom["spriteNaming"] = recomender.recomender_sprite(result_sprite_naming)
                if 'backdropNaming' in dict_analysis:
                    dict_recom["backdropNaming"] = recomender.recomender_backdrop(result_backdrop_naming)
                dict_analysis.update(proc_recomender(dict_recom))
            except Exception: pass
            
    except Exception as e:
        logger.error(f"Critical error analyzing project: {e}")
        return {'Error': 'critical_error'}
    
    if 'Error' not in dict_analysis:
        dict_analysis['Error'] = 'None'

    return dict_analysis

# ==============================================================================
# 4. FUNCIONES DE ENTRADA (VIEWS)
# ==============================================================================

def analysis_by_upload(request, skill_points: dict, upload):
    try:
        original_name = upload.name
        safe_name = original_name[:95] if len(original_name) > 95 else original_name
        zip_filename = safe_name.encode('utf-8', 'ignore')
        
        filename_obj = save_analysis_in_file_db(request, zip_filename)
        
        dir_zips = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads/")
        if not os.path.exists(dir_zips):
            os.makedirs(dir_zips)

        project_name = str(uuid.uuid4()) 
        unique_id = '{}_{}{}'.format(project_name, datetime.now().strftime("%Y_%m_%d_%H_%M_%S_"), datetime.now().microsecond)
        file_saved = os.path.join(dir_zips, unique_id + ".sb2")
        
        info_project = {'platform': 'Snap', 'username': "", 'projectname': ''}
        write_activity_in_logfile(filename_obj)

        request.session['current_project_path'] = file_saved
        with open(file_saved, 'wb+') as destination:
            for chunk in upload.chunks():
                destination.write(chunk)
                
        dict_drscratch_analysis = analyze_project(request, info_project, skill_points, file_saved, filename_obj)

        if not dict_drscratch_analysis:
             dict_drscratch_analysis = {'Error': 'empty_result'}
            
        dict_drscratch_analysis.update({
            'url': None,
            'filename': original_name,
            'dashboard_mode': request.POST.get('dashboard_mode'),
            'multiproject': False
        })
        return dict_drscratch_analysis

    except Exception as e:
        traceback.print_exc()
        return {
            'filename': upload.name if upload else "unknown",
            'Error': 'analyzing',
            'dashboard_mode': request.POST.get('dashboard_mode')
        }

def analysis_by_url(request, url, skill_points: dict):
    try:
        info_project = return_scratch_project_identifier(url)
        if info_project['platform'] == "error":
            return {'Error': 'id_error'}
        else:
            dic = generator_dic(request, info_project, skill_points)
            dic.update({
                'url': url,
                'filename': url,
                'dashboard_mode': request.POST.get('dashboard_mode', 'Default'),
                'multiproject': False
            })
            return dic
    except Exception:
        return {'Error': 'analyzing'}

def generator_dic(request, info_project, skill_points: dict) -> dict:
    try:
        username = None
        # AQUÍ ES DONDE SE CREA EL OBJETO DB
        file_obj = send_request_getsb3(info_project['projectname'], username, method="url")
    except Exception as e:
        logger.error(f"Error creating DB file object: {e}")
        return {'Error': 'no_exists'} # Esto provoca la redirección al main si falla la DB

    try:
        filename_obj = ""
        d = analyze_project(request, info_project, skill_points, filename_obj, file_obj)
    except Exception:
        return {'Error': 'analyzing'}

    return d

def _make_compare(request, skill_points: dict):
    counter = 0
    d = {}
    path = {}
    json_projects = {}
    
    if request.method != "POST":
        return HttpResponseRedirect('/')

    if "_urls" in request.POST:
        for url in request.POST.getlist('urlProject'):
            project = check_project(counter)
            d[project] = analysis_by_url(request, url, skill_points)
            path[project] = request.session.get('current_project_path')
            counter += 1
    elif "_uploads" in request.POST:
        for upload in request.FILES.getlist('zipFile'):
            project = check_project(counter)
            d[project] = analysis_by_upload(request, skill_points, upload)
            path[project] = request.session.get('current_project_path')
            counter += 1

    for key, value in path.items():
        json_projects[key] = load_json_project(value)    
    
    dict_scratch_golfing = ScratchGolfing(json_projects.get('Original'), json_projects.get('New')).finalize()
    d['Compare'] = dict_scratch_golfing['result']['scratch_golfing']
    check_same_functionality(request, d)

    return d

# ==============================================================================
# 5. FUNCIONES DE SOPORTE Y PARSEO XML (CORE)
# ==============================================================================

def load_json_project(path_projectsb3):
    try:
       with open(path_projectsb3, "r", encoding="utf-8") as archivo_xml:
        return archivo_xml.read()
    except Exception: return ""

def get_snap_project_xml(username, projectname):
    # --- CORRECCIÓN CRUCIAL PARA URLS ---
    safe_user = quote(username)
    safe_proj = quote(projectname)
    url = f"https://snap.berkeley.edu/api/v1/projects/{safe_user}/{safe_proj}"
    try:
        # Añadido HEADER para evitar bloqueo de Snap
        headers = {'User-Agent': 'DrSnap-Analyzer/1.0', 'Accept': 'application/json'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        try:
            data = response.json()
            if 'xml' in data: return data['xml']
            if 'code' in data: return data['code']
            return response.text
        except json.JSONDecodeError: return response.text
    except Exception:
        raise DrScratchException(f"Could not download project.")

def send_request_getsb3(id_project, username, method):
    file_url = f'{id_project}.sb3'[:95]
    now = datetime.now()
    
    # Determinar propietario
    if Organization.objects.filter(username=username).exists():
        file_obj = File(filename=file_url, organization=username, method=method, time=now)
    elif Coder.objects.filter(username=username).exists():
        file_obj = File(filename=file_url, coder=username, method=method, time=now)
    else:
        file_obj = File(filename=file_url, method=method, time=now)

    # --- CORRECCIÓN DE BASE DE DATOS CRÍTICA ---
    # Inicializamos todos los campos numéricos a 0 antes de guardar.
    # Esto evita el error "Column 'score' cannot be null".
    attributes = ['score', 'abstraction', 'parallelization', 'logic', 'synchronization', 
                  'flowControl', 'userInteractivity', 'dataRepresentation', 'spriteNaming', 
                  'initialization', 'deadCode', 'duplicateScript']
    
    for attr in attributes:
        setattr(file_obj, attr, 0)
    
    file_obj.save()
    write_activity_in_logfile(file_obj)
    return file_obj

def parse_snap_script(script_node, scene_name, dict_datos, id_counter):
    """
    NUEVA FUNCIÓN: Procesa un <script> de Snap! y enlaza los bloques secuenciales.
    Sustituye a la antigua función get_blocks.
    """
    # Filtrar solo elementos que sean bloques o eventos (hat blocks)
    elements = [child for child in script_node if child.tag == 'block' or child.tag.startswith('receive')]
            
    if not elements:
        return id_counter

    previous_block_id = None
    
    for elem in elements:
        # 1. Extraer nombre del bloque
        if elem.tag.startswith('receive'):
            block_name = elem.tag
        else:
            block_name = elem.get('s')
            
        block_id = str(id_counter)
        id_counter += 1
        
        block_data = {'block': block_name, 'id': block_id, 'next': []}
        
        # 2. Extraer opciones del bloque (menús desplegables) si tiene
        trigo_blocks = elem.find(".//option")
        if trigo_blocks is not None:
            block_data['option'] = trigo_blocks.text

        # 3. Lo añadimos a la lista del objeto
        dict_datos[scene_name]['blocks'].append(block_data)
        
        # 4. LA MAGIA: Enlazarlo secuencialmente con el bloque que tiene justo encima
        if previous_block_id is not None:
            for b in dict_datos[scene_name]['blocks']:
                if b['id'] == previous_block_id:
                    b['next'].append(block_id)
                    break
        
        previous_block_id = block_id
        
        # 5. Si es un bloque con forma de C (bucle, if), procesar su interior
        nested_scripts = elem.findall('script')
        for n_script in nested_scripts:
            # Si el script interno tiene bloques, los enlazamos como hijos
            inner_elements = [c for c in n_script if c.tag == 'block' or c.tag.startswith('receive')]
            if inner_elements:
                block_data['next'].append(str(id_counter))
            
            # Llamada recursiva para procesar el interior
            id_counter = parse_snap_script(n_script, scene_name, dict_datos, id_counter)
            
    return id_counter

def split_xml(request, scratch_project_inf):
    from lxml import etree
    dict_datos = {}
    try:
        if not scratch_project_inf: return {}
        if isinstance(scratch_project_inf, str):
            scratch_project_inf = scratch_project_inf.replace('<?xml version="1.0" encoding="UTF-8"?>', '')
            scratch_project_inf = scratch_project_inf.strip()

        parser = etree.XMLParser(recover=True)
        root = etree.fromstring(scratch_project_inf.encode('utf-8'), parser=parser)
        
        if root.tag == 'project': project = root
        else: project = root.find('project')

        if project is not None:
            id_counter = 0
            stages = []
            stages.extend(project.findall('.//stage')) 
            if not stages: stages.extend(project.findall('stage')) 
            
            for stage in stages:
                stage_name = stage.get('name') 
                if stage_name:
                     if stage_name not in dict_datos: dict_datos[stage_name] = {'blocks': [], 'costumes': []}
                     
                     # Procesar todos los scripts del escenario
                     for scripts in stage.findall('scripts'):
                        for script in scripts.findall('script'):
                            id_counter = parse_snap_script(script, stage_name, dict_datos, id_counter)          
                            
                     # Procesar disfraces
                     for costumes in stage.findall('costumes'):
                        for lis in costumes.findall('list'):
                            for item in lis.findall('item'):
                                costume = item.find('ref')
                                if costume is not None:
                                    c_name = costume.get('mediaID')
                                    if c_name: dict_datos[stage_name]['costumes'].append(c_name)
                     
                     # Procesar los objetos (sprites)
                     for sprites in stage.findall('sprites'):
                        for sprite in sprites.findall('sprite'):
                            sprite_name = sprite.get('name') 
                            if sprite_name not in dict_datos: dict_datos[sprite_name] = {'blocks': [], 'costumes': []}
                            
                            for costumes in sprite.findall('costumes'):
                                for lis in costumes.findall('list'):
                                    for item in lis.findall('item'):
                                        costume = item.find('ref')
                                        if costume is not None:
                                            c_name = costume.get('mediaID')
                                            if c_name: dict_datos[sprite_name]['costumes'].append(c_name)
                                            
                            # Procesar todos los scripts del objeto
                            for scripts in sprite.findall('scripts'):
                                for script in scripts.findall('script'):
                                    id_counter = parse_snap_script(script, sprite_name, dict_datos, id_counter)          

    except Exception as e:
        logger.error(f"Error splitting XML: {e}")
        
    return dict_datos

# Funciones extra
def return_scratch_project_identifier(url) -> dict:
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)
    username = query_params.get('username', [None])[0]
    projectname = query_params.get('projectname', [None])[0]
    if username and projectname:
        return {'platform': 'Snap', 'username': username, 'projectname': projectname}
    else:
        return {'platform': 'error', 'message': 'Missing username/projectname'}

def write_activity_in_logfile(file_name):
    try:
        log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'log', 'logFile.txt')
        with open(log_path, "a+") as log_file:
            log_file.write(f"ID: {file_name.id}\n")
    except Exception: pass

def save_analysis_in_file_db(request, zip_filename):
    now = datetime.now()
    safe_name = zip_filename[:95] if len(zip_filename) > 95 else zip_filename
    if request.user.is_authenticated and Organization.objects.filter(username=request.user.username).exists():
        f = File(filename=safe_name, organization=request.user.username, method="project", time=now)
    else:
        f = File(filename=safe_name, method="project", time=now)
    for attr in ['score', 'abstraction', 'parallelization', 'logic', 'synchronization', 
                 'flowControl', 'userInteractivity', 'dataRepresentation', 'spriteNaming', 
                 'initialization', 'deadCode', 'duplicateScript']:
        if hasattr(f, attr): setattr(f, attr, 0)
    f.save()
    return f

def check_project(counter): return "Original" if counter == 0 else "New"
def check_same_functionality(request, d): pass
def check_version(filename): return '2.0'
def proc_urls(request, dict_mastery, file_obj): return {}
def get_urls(dict_mastery): return []
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
# Imports de análisis (Hairball)
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

logger = logging.getLogger(__name__)

def save_analysis_in_file_db(request, zip_filename):
    now = datetime.now()
    method = "project"

    if request.user.is_authenticated:
        username = request.user.username
    else:
        username = None

    if Organization.objects.filter(username=username).exists():
        filename_obj = File(filename=zip_filename, organization=username, method=method, time=now)
    elif Coder.objects.filter(username=username).exists():
        filename_obj = File(filename=zip_filename, coder=username, method=method, time=now)
    else:
        filename_obj = File(filename=zip_filename, method=method, time=now)

    for attr in ['score', 'abstraction', 'parallelization', 'logic', 'synchronization', 
                 'flowControl', 'userInteractivity', 'dataRepresentation', 'spriteNaming', 
                 'initialization', 'deadCode', 'duplicateScript']:
        setattr(filename_obj, attr, 0)

    filename_obj.save()
    return filename_obj

def _make_compare(request, skill_points: dict):
    """
    Make comparison of two projects
    """
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
    elif "_mix" in request.POST:
        project = check_project(counter)
        base_type = request.POST.get('baseProjectType')
        if base_type == "urlProject":
            url = request.POST.getlist('urlProject')[0]
            d[project] = analysis_by_url(request, url, skill_points)
            path[project] = request.session.get('current_project_path')
            counter += 1
            upload = request.FILES.get('zipFile')
            project = check_project(counter)
            d[project] = analysis_by_upload(request, skill_points, upload)
            path[project] = request.session.get('current_project_path')
        else:
            upload = request.FILES.get('zipFile')
            d[project] = analysis_by_upload(request, skill_points, upload)
            path[project] = request.session.get('current_project_path')
            counter += 1
            project = check_project(counter)
            url = request.POST.getlist('urlProject')[1]
            d[project] = analysis_by_url(request, url, skill_points)
            path[project] = request.session.get('current_project_path')

    for key, value in path.items():
        json_projects[key] = load_json_project(value)    
    
    dict_scratch_golfing = ScratchGolfing(json_projects.get('Original'), json_projects.get('New')).finalize()
    d['Compare'] = dict_scratch_golfing['result']['scratch_golfing']
    check_same_functionality(request, d)

    return d
    
def check_project(counter):
    return "Original" if counter == 0 else "New"

def check_same_functionality(request, d):
    same_functionality = request.POST.get('same_functionality') == "True"
    d['Compare'].update({'same_functionality': same_functionality})

def return_scratch_project_identifier(url) -> dict:
    """
    Extract username and projectname from Snap! URL
    """
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)
    
    username = query_params.get('username', [None])[0]
    projectname = query_params.get('projectname', [None])[0]

    if username and projectname:
        return {'platform': 'Snap', 'username': username, 'projectname': projectname}
    else:
        return {'platform': 'error', 'message': 'Missing username or projectname in URL'}

def write_activity_in_logfile(file_name):
    log_filename = '{}/log/{}'.format(os.path.dirname(os.path.dirname(__file__)), 'logFile.txt')
    try:
        with open(log_filename, "a+") as log_file:
            log_file.write(f"FileName: {file_name.filename}\t\t\tID: {file_name.id}\t\t\tMethod: {file_name.method}\t\t\tTime: {file_name.time}\n")
    except Exception as e:
        logger.error(f"Error writing log file: {e}")

def generate_uniqueid_for_saving(id_project):
    date_now = datetime.now()
    date_now_string = date_now.strftime("%Y_%m_%d_%H_%M_%S_%f")
    return id_project + "_" + date_now_string

def load_json_project(path_projectsb3):
    try:
       with open(path_projectsb3, "r", encoding="utf-8") as archivo_xml:
        return archivo_xml.read()
    except Exception as e:
        logger.error(f"Error loading JSON project: {e}")
        return ""

def proc_recomender(dict_recom):
    recomender = {
        'recomenderSystem': {
            'message': "Congrat's you don't have any bad smell at the moment.",
        }
    }
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

def proc_urls(request, dict_mastery, file_obj):
    dict_urls = {}
    mode = request.POST.get('dashboard_mode', 'Default')
    non_personalized = ['Default', 'Comparison', 'Recommender']

    if mode not in non_personalized:
        dict_extended = dict_mastery['extended'].copy()
        dict_vanilla = dict_mastery['vanilla'].copy()
        dict_urls["url_extended"] = get_urls(dict_extended)
        dict_urls["url_vanilla"] = get_urls(dict_vanilla)
    elif mode == 'Personalized':
        dict_personal = dict_mastery['personalized'].copy()
        dict_urls["url_personal"] = get_urls(dict_personal)
    return dict_urls

def get_urls(dict_mastery):
    return [key for key in dict_mastery.keys() 
            if key not in ['total_points', 'competence', 'max_points', 'average_points']]

def proc_mastery(request, dict_mastery, file_obj):
    dic = {}
    mode = request.POST.get('dashboard_mode', 'Default')
    non_personalized = ['Default', 'Comparison', 'Recommender']
    
    if mode in non_personalized:
        dict_extended = dict_mastery['extended'].copy()
        dict_vanilla = dict_mastery['vanilla'].copy()
        set_file_obj(request, file_obj, dict_extended)
        set_file_obj(request, file_obj, dict_vanilla, 'Vanilla')
        d_extended_translated = translate(request, dict_extended, file_obj)
        d_vanilla_translated = translate(request, dict_vanilla, file_obj, vanilla=True)
        dic = {"mastery": d_extended_translated, "mastery_vanilla": d_vanilla_translated}
        dic["mastery"]["competence"] = dict_extended["competence"]
        dic["mastery"]["points"] = dict_extended["total_points"]
        dic["mastery_vanilla"]["competence"] = dict_vanilla["competence"]
        dic["mastery_vanilla"]["points"] = dict_vanilla["total_points"]     
    elif mode == 'Personalized':
        dict_personal = dict_mastery['personalized'].copy()
        set_file_obj(request, file_obj, dict_personal)
        d_personal_translated = translate(request, dict_personal, file_obj)
        dic = {"mastery": d_personal_translated}
        dic["mastery"]["competence"] = dict_personal["competence"]
        dic["mastery"]["points"] = dict_personal["total_points"]
    
    return dic

def set_file_obj(request, file_obj, dict_data, mode=None):
    file_obj.score = dict_data["total_points"][0]
    file_obj.competence = dict_data["competence"]
    file_obj.abstraction = dict_data["Abstraction"][0]
    file_obj.parallelization = dict_data["Parallelization"][0]
    file_obj.logic = dict_data["Logic"][0]
    file_obj.synchronization = dict_data["Synchronization"][0]
    file_obj.flow_control = dict_data["FlowControl"][0]
    file_obj.userInteractivity = dict_data["UserInteractivity"][0]
    file_obj.dataRepresentation = dict_data["DataRepresentation"][0]
    if mode != 'Vanilla':
        file_obj.mathOperators = dict_data["MathOperators"][0]
        file_obj.mathOperators = dict_data["MotionOperators"][0] 
    file_obj.save()

def proc_duplicate_script(dict_result, file_obj) -> dict:
    dict_ds = {}
    dict_ds["duplicateScript"] = {
        "number": dict_result['result']['total_duplicate_scripts'],
        "scripts": dict_result['result']['list_duplicate_scripts'],
        "csv_format": dict_result['result']['list_csv']
    }
    file_obj.duplicateScript = dict_result['result']['total_duplicate_scripts']
    file_obj.save()
    return dict_ds

def proc_dead_code(dict_dead_code, filename):
    dict_dc = {"deadCode": {"number": dict_dead_code['result']['total_dead_code_scripts']}}

    for dict_sprite_dead_code_blocks in dict_dead_code['result']['list_dead_code_scripts']:
        for sprite_name, list_blocks in dict_sprite_dead_code_blocks.items():
            dict_dc["deadCode"][sprite_name] = list_blocks

    filename.deadCode = dict_dead_code['result']['total_dead_code_scripts']
    filename.save()
    return dict_dc

def proc_sprite_naming(lines, file_obj):
    dic = {}
    lLines = lines.split('\n')
    number = lLines[0].split(' ')[0]
    lObjects = lLines[1:]
    lfinal = lObjects[:-1]

    dic['spriteNaming'] = {'number': int(number), 'sprite': lfinal}
    file_obj.spriteNaming = number
    file_obj.save()
    return dic

def proc_backdrop_naming(lines, file_obj):
    dic = {}
    lLines = lines.split('\n')
    number = lLines[0].split(' ')[0]
    lObjects = lLines[1:]
    lfinal = lObjects[:-1]
    
    dic['backdropNaming'] = {'number': int(number), 'backdrop': lfinal}
    file_obj.backdropNaming = number
    file_obj.save()
    return dic

def proc_block_sprite_usage(result_block_sprite_usage, filename):
    return {"block_sprite_usage": result_block_sprite_usage}

def translate(request, d, filename, vanilla=False):
    """
    Translate the output of Hairball based on LANGUAGE_CODE
    """
    # 1. Obtenemos el código y lo limpiamos (es-es -> es)
    raw_lang = request.LANGUAGE_CODE
    lang = raw_lang.split('-')[0] if '-' in raw_lang else raw_lang

    translations = {
        "es": {'Abstraction': 'Abstracción', 'Parallelization': 'Paralelismo', 'Logic': 'Pensamiento lógico', 'Synchronization': 'Sincronización', 'FlowControl': 'Control de flujo', 'UserInteractivity': 'Interactividad con el usuario', 'DataRepresentation': 'Representación de la información', 'MathOperators': 'Operadores matemáticos', 'MotionOperators': 'Operadores de movimiento'},
        "en": {'Abstraction': 'Abstraction', 'Parallelization': 'Parallelism', 'Logic': 'Logic', 'Synchronization': 'Synchronization', 'FlowControl': 'Flow control', 'UserInteractivity': 'User interactivity', 'DataRepresentation': 'Data representation', 'MathOperators': 'Math operators', 'MotionOperators': 'Motion operators'},
        "gl": {'Abstraction': 'Abstracción', 'Parallelization': 'Paralelismo', 'Logic': 'Pensamento lóxico', 'Synchronization': 'Sincronización', 'FlowControl': 'Control de fluxo', 'UserInteractivity': 'Interactividade co usuario', 'DataRepresentation': 'Representación da información', 'MathOperators': 'Operadores matemáticos', 'MotionOperators': 'Operadores de movemento'},
        "ca": {'Abstraction': 'Abstracció', 'Parallelization': 'Paral·lelisme', 'Logic': 'Pensament lògic', 'Synchronization': 'Sincronització', 'FlowControl': 'Control de flux', 'UserInteractivity': "Interactivitat amb l'usuari", 'DataRepresentation': "Representació de la informació", 'MathOperators': 'Operadors matemàtics', 'MotionOperators': 'Operadors de moviment'},
        "eu": {'Abstraction': 'Abstrakzioa', 'Parallelization': 'Paralelismoa', 'Logic': 'Pentsamendu logikoa', 'Synchronization': 'Sinkronizazioa', 'FlowControl': 'Fluxu kontrola', 'UserInteractivity': 'Erabiltzailearen interaktibitatea', 'DataRepresentation': 'Informazioaren errepresentazioa', 'MathOperators': 'Operadore matematikoak', 'MotionOperators': 'Mugimendu operadoreak'},
    }

    # 2. Usamos 'get' para evitar errores si el idioma no existe (fallback a inglés)
    t_map = translations.get(lang, translations['en'])
    
    result = {}
    keys = ['Abstraction', 'Parallelization', 'Logic', 'Synchronization', 'FlowControl', 'UserInteractivity', 'DataRepresentation']
    if not vanilla:
        keys.extend(['MathOperators', 'MotionOperators'])

    for key in keys:
        # Si la clave no está en el mapa, usa la original en inglés
        result[t_map.get(key, key)] = [d[key], key]

    filename.language = lang
    filename.save()
    return result

def get_blocks(block, scene_name, dict_datos, parent_id):
    block_name = block.get('s')  
    block_id = parent_id 

    trigo_blocks = block.find(".//option")
    if trigo_blocks is not None: 
        trigo_value = trigo_blocks.text
        block_data = {'block': block_name, 'id': block_id,  'option': trigo_value} 
    else:
        block_data = {'block': block_name, 'id': block_id} 
    
    child_blocks = block.findall('block[@s]')
    script_blocks = block.findall('script')
    for script in script_blocks:
        child_blocks.extend(script.findall('./block[@s]'))
    
    if child_blocks:
        block_data['next'] = []
        for index, child_block in enumerate(child_blocks, start=1):
            child_id = f"{block_id}.{index}"
            child_data, _ = get_blocks(child_block, scene_name, dict_datos, child_id)
            block_data['next'].append(child_id)
    
    dict_datos[scene_name]['blocks'].append(block_data)
    return dict_datos, block_id
    
def split_xml(request, scratch_project_inf):
    dict_datos = {}
    try:
        if not scratch_project_inf:
            raise ValueError("Empty project content")
            
        root = etree.fromstring(scratch_project_inf)
        project = root.find('project')
        id_counter = 0

        if project is not None:
            # Parse Scenes
            for scenes in project.findall('scenes'):
                for scene in scenes.findall('scene'):
                    scene_name = scene.get('name') 
                    if scene_name:
                        dict_datos[scene_name] = {'blocks': []}
                    for blocks in scene.findall('blocks'):
                        for block_def in blocks.findall('block-definition'):
                            for script in block_def.findall('script'):
                                for block in script.findall('./block'):
                                    dict_datos, id_counter = get_blocks(block, scene_name, dict_datos, id_counter) 
                                    id_counter += 1       

            # Parse Stages
            for scenes in project.findall('scenes'):
                for scene in scenes.findall('scene'):
                    for stage in scene.findall('stage'):
                        stage_name = stage.get('name') 
                        if stage_name:
                            dict_datos[stage_name] = {'blocks': []}
                            for scripts in stage.findall('scripts'):
                                for script in scripts.findall('script'):
                                    for block in script.findall('./block'):
                                        dict_datos, id_counter = get_blocks(block, stage_name, dict_datos, id_counter)          
                                        id_counter += 1 
            
            # Parse Sprites and Costumes
            for scenes in project.findall('scenes'):
                for scene in scenes.findall('scene'):
                    for stage in scene.findall('stage'):
                        for sprites in stage.findall('sprites'):
                            for sprite in sprites.findall('sprite'):
                                sprite_name = sprite.get('name') 
                                dict_datos[sprite_name] = {'blocks': [], 'costumes': []}
                                
                                # Costumes
                                for costumes in sprite.findall('costumes'):
                                    for lis in costumes.findall('list'):
                                        for item in lis.findall('item'):
                                            costume = item.find('ref')
                                            if costume is not None:
                                                costume_name = costume.get('mediaID')
                                                if costume_name:
                                                    dict_datos[sprite_name]['costumes'].append(costume_name)

                                # Blocks
                                for scripts in sprite.findall('scripts'):
                                    for script in scripts.findall('script'):
                                        for block in script.findall('./block'):
                                            dict_datos, id_counter = get_blocks(block, sprite_name, dict_datos, id_counter)          
                                            id_counter += 1 
    except Exception as e:
        logger.error(f"Error splitting XML: {e}")
        
    return dict_datos


def get_snap_project_xml(username, projectname):

    safe_user = quote(username)
    safe_proj = quote(projectname)
    url = f"https://snap.berkeley.edu/api/v1/projects/{safe_user}/{safe_proj}"

    try:
        logger.info(f"Downloading project from: {url}")
        headers = {
            'User-Agent': 'DrSnap-Analyzer/1.0',
            'Accept': 'application/json'
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        try:
            data = response.json()
            # Prioritize fields based on API structure
            if 'xml' in data: return data['xml']
            if 'code' in data: return data['code']
            if 'SourceCode' in data: return data['SourceCode']
            
            logger.warning("JSON received but no xml/code field found. Returning raw text.")
            return response.text
        except json.JSONDecodeError:
            # Fallback for non-JSON responses
            return response.text

    except requests.exceptions.RequestException as e:
        logger.error(f"Error connecting to Snap API v1: {e}")
        raise DrScratchException(f"Could not download project. Verify '{projectname}' is PUBLIC. Error: {e}")


def send_request_getsb3(id_project, username, method):
    file_url = f'{id_project}.sb3'
    now = datetime.now()

    # Determine ownership
    if Organization.objects.filter(username=username).exists():
        file_obj = File(filename=file_url, organization=username, method=method, time=now)
    elif Coder.objects.filter(username=username).exists():
        file_obj = File(filename=file_url, coder=username, method=method, time=now)
    else:
        file_obj = File(filename=file_url, method=method, time=now)

    # Initialize stats to 0
    for attr in ['score', 'abstraction', 'parallelization', 'logic', 'synchronization', 
                 'flowControl', 'userInteractivity', 'dataRepresentation', 'spriteNaming', 
                 'initialization', 'deadCode', 'duplicateScript']:
        setattr(file_obj, attr, 0)
    
    file_obj.save()
    write_activity_in_logfile(file_obj)
    return file_obj


def analyze_project(request, info_project, skill_points: dict, filename_obj, file_obj):
    dict_analysis = {}
    dashboard = request.POST.get('dashboard_mode', 'Default')
    curr_type = request.POST.get('curr_type', '')

    # --- Loading Project Content ---
    if info_project.get("projectname"):
        # Download from Snap API
        try:
            scratch_project_inf = get_snap_project_xml(
                info_project['username'], 
                info_project['projectname']
            )
        except Exception as e:
             logger.error(f"Fatal error downloading: {e}")
             return {'Error': 'no_exists'}
    else:
        # Load local file
        scratch_project_inf = load_json_project(filename_obj)

    # --- XML Parsing ---
    json_snap_project = split_xml(request, scratch_project_inf)
    path_projectsb3 = info_project["projectname"] if info_project.get("projectname") else "upload"

    # --- Hairball Analysis ---
    dict_mastery = Mastery(path_projectsb3, json_snap_project, skill_points, dashboard).finalize()
    dict_dead_code = DeadCode(path_projectsb3, json_snap_project).finalize()
    result_sprite_naming = SpriteNaming(path_projectsb3, json_snap_project).finalize()
    result_backdrop_naming = BackdropNaming(path_projectsb3, json_snap_project).finalize()
    result_block_sprite_usage = Block_Sprite_Usage(path_projectsb3, json_snap_project).finalize()
    
    # --- Recommender System ---
    if dashboard == 'Recommender':
        dict_recom = {}
        recomender = RecomenderSystem(curr_type)
        dict_recom["deadCode"] = recomender.recomender_deadcode(dict_dead_code)
        dict_recom["spriteNaming"] = recomender.recomender_sprite(result_sprite_naming)
        dict_recom["backdropNaming"] = recomender.recomender_backdrop(result_backdrop_naming)
        
        dict_analysis.update(proc_recomender(dict_recom))
   
    # --- Aggregating Results ---
    dict_analysis.update(proc_mastery(request, dict_mastery, file_obj))
    dict_analysis.update(proc_dead_code(dict_dead_code, file_obj))
    dict_analysis.update(proc_sprite_naming(result_sprite_naming, file_obj))
    dict_analysis.update(proc_backdrop_naming(result_backdrop_naming, file_obj))
    dict_analysis.update(proc_block_sprite_usage(result_block_sprite_usage, file_obj))
    
    return dict_analysis

    
def analysis_by_upload(request, skill_points: dict, upload):
    """
    Upload file from form POST for unregistered users
    """
    zip_filename = upload.name.encode('utf-8')
    filename_obj = save_analysis_in_file_db(request, zip_filename)
    
    dir_zips = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads/")
    if not os.path.exists(dir_zips):
        os.makedirs(dir_zips)

    project_name = str(uuid.uuid4()) 
    unique_id = '{}_{}{}'.format(project_name, datetime.now().strftime("%Y_%m_%d_%H_%M_%S_"), datetime.now().microsecond)
    zip_filename_str = zip_filename.decode('utf-8')
    
    # Check version (Simplified default to 2.0/Snap)
    version = check_version(zip_filename_str)
    file_saved = os.path.join(dir_zips, unique_id + ".sb2")
    
    info_project = {'platform': 'Snap', 'username': "", 'projectname': ''}
    
    # Log Upload
    write_activity_in_logfile(filename_obj)

    # Save file to server
    request.session['current_project_path'] = file_saved
    with open(file_saved, 'wb+') as destination:
        for chunk in upload.chunks():
            destination.write(chunk)
            
    try:
        dict_drscratch_analysis = analyze_project(request, info_project, skill_points, file_saved, filename_obj)
    except Exception:
        traceback.print_exc()
        filename_obj.method = 'project/error'
        filename_obj.save()
        
        # Move to error folder
        error_dir = file_saved.split("/uploads/")[0] + "/error_analyzing/"
        if not os.path.exists(error_dir):
            os.makedirs(error_dir)
        new_path_project = error_dir + file_saved.split("/uploads/")[1]
        shutil.copy(file_saved, new_path_project)
        
        return {
            'filename': upload.name,
            'Error': 'analyzing',
            'dashboard_mode': request.POST.get('dashboard_mode')
        }

    dict_drscratch_analysis['Error'] = 'None'
    dict_drscratch_analysis.update({
        'url': None,
        'filename': upload.name,
        'dashboard_mode': request.POST.get('dashboard_mode'),
        'multiproject': False
    })
    return dict_drscratch_analysis

    
def analysis_by_url(request, url, skill_points: dict):
    """
    Make the automatic analysis by URL
    """
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

def generator_dic(request, info_project, skill_points: dict) -> dict:
    """
    Return a dictionary with static analysis and errors
    """
    try:
        username = None
        # Create DB entry for the URL analysis
        file_obj = send_request_getsb3(info_project['projectname'], username, method="url")
    except DrScratchException:
        logger.error('DrScratchException creating file obj')
        return {'Error': 'no_exists'}
    except Exception as e:
        logger.error(f'Error initializing analysis: {e}')
        return {'Error': 'no_exists'}

    try:
        filename_obj = ""
        d = analyze_project(request, info_project, skill_points, filename_obj, file_obj)
    except Exception as e:
        logger.error(f'Impossible analyze project: {e}')
        return {'Error': 'analyzing'}

    d['Error'] = 'None'
    return d

def check_version(filename):
    extension = filename.split('.')[-1]
    if extension == 'sb2':
        return '2.0'
    elif extension == 'sb3':
        return '3.0'
    else:
        return '1.4'
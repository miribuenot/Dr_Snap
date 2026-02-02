#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import os
import json
import uuid
import zipfile
import shutil
import unicodedata
import logging
import coloredlogs
import re
import csv
import tempfile
from datetime import datetime, timedelta, date
from zipfile import ZipFile, BadZipfile

# Django imports
from django.http import HttpResponseRedirect, HttpResponse, JsonResponse, FileResponse
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.contrib import messages
from django.contrib.auth import logout, login, authenticate, get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes, smart_str
from django.utils.datastructures import MultiValueDictKeyError
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils import timezone
from django.db.models import Avg
from django.contrib.auth.models import User
from django.shortcuts import render, get_object_or_404, redirect
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.core.files.uploadedfile import SimpleUploadedFile

# App imports (Modelos y Formularios)
from .models import BatchCSV, File, CSVs, Organization, OrganizationHash, Coder, Discuss, Stats
from app.forms import UrlForm, OrganizationForm, OrganizationHashForm, LoginOrganizationForm, CoderForm, DiscussForm
from app.pyploma import generate_certificate
from app.hairball3.scratchGolfing import ScratchGolfing

# Analyzer imports (CRUCIAL: Estas son las funciones que arreglamos para Snap!)
from .analyzer import (
    analyze_project, 
    generator_dic, 
    return_scratch_project_identifier, 
    send_request_getsb3, 
    _make_compare, 
    analysis_by_upload, 
    analysis_by_url
)

# Tasks & Batch utils
from .tasks import init_batch
from .batch import skills_translation
from . import batch as batch_utils 

# Configuración de Logs
logger = logging.getLogger(__name__)
coloredlogs.install(level='DEBUG', logger=logger)
supported_languages = ['es', 'ca', 'gl', 'pt']


# ==============================================================================
# 1. VISTAS PRINCIPALES
# ==============================================================================

def main(request):
    if request.user.is_authenticated:
        username = request.user.username
        user_type = identify_user_type(request)
        is_admin = identify_admin(user_type)

        if is_admin:
            return render(request, 'main/main.html', {'username': username})
        
        try:
            if user_type == 'coder':
                user_obj = Coder.objects.get(username=username)
            elif user_type == 'organization':
                user_obj = Organization.objects.get(username=username)
            else:
                return render(request, 'main/main.html', {'username': username})
            
            return render(request, user_type + '/main.html', {'username': username, "img": str(user_obj.img)})
        except (Coder.DoesNotExist, Organization.DoesNotExist):
            return render(request, 'main/main.html', {'username': username})
    else:
        return render(request, 'main/main.html', {'username': None})

def contest(request):
    return render(request, 'contest.html', {})

def collaborators(request):
    return render(request, 'main/collaborators.html')

def process_contact_form(request):
    """
    Procesa el formulario de contacto y envía un correo a la administración.
    """
    if request.method != 'POST':
        return HttpResponse('METHOD NOT ALLOWED', status=405)

    # 1. Validación manual de campos
    required_fields = {
        'contact_name': 'Please, fill your name.',
        'contact_email': 'Please, fill your email.',
        'contact_text': 'Please, fill the text area.'
    }
    
    for field, error_message in required_fields.items():
        value = request.POST.get(field, '')
        if not value or not value.strip(): # .strip() evita que pasen espacios en blanco
            messages.error(request, error_message)
            request.session['form_data'] = request.POST
            return HttpResponseRedirect('/contact')
    
    # 2. Extracción de datos
    contact_name = request.POST.get('contact_name')
    contact_email = request.POST.get('contact_email')
    contact_text = request.POST.get('contact_text')
    contact_media = request.FILES.get('contact_media')

    # 3. Construcción del Email
    subject = f'[CONTACT FORM] Message from {contact_name}'
    message = f"Name: {contact_name}\nEmail: {contact_email}\n\nMessage:\n{contact_text}"
    
    email = EmailMessage(
        subject, 
        message, 
        settings.EMAIL_HOST_USER, 
        ['drscratch@gsyc.urjc.es'],
        headers={'Reply-To': contact_email} # Permite responder directamente al usuario
    )
    
    if contact_media:
        email.attach(contact_media.name, contact_media.read(), contact_media.content_type)
    
    # 4. Envío seguro
    try:
        email.send()
        messages.success(request, "Message sent successfully!")
    except Exception as e:
        logger.error(f"Error sending contact email: {e}")
        messages.error(request, "Error sending message. Please try again later.")

    return HttpResponseRedirect('/')    

def contact(request):
    return render(request, 'main/contact-form.html') 

def learn(request, page):
    """ Renderiza las páginas de tutoriales traduciendo el slug de la URL """
    # Diccionario de traducción de habilidades (skills_translation viene de app.batch)
    translation_map = skills_translation(request)
    
    # Si la página solicitada tiene traducción, usamos la traducida
    if page in translation_map: 
        page = translation_map[page]
    
    template_path = f'learn/{page}.html'
    
    context = {
        'flagUser': 1 if request.user.is_authenticated else 0
    }

    if request.user.is_authenticated:
        user_type = identify_user_type(request)
        context.update({
            'user': user_type,
            'username': request.user.username
        })
        
    return render(request, template_path, context)

def discuss(request):
    """ Foro de discusión/feedback """
    if request.method == "POST":
        form = DiscussForm(request.POST)
        if form.is_valid():
            nick = request.user.username if request.user.is_authenticated else ""
            Discuss.objects.create(
                nick=nick, 
                date=timezone.now(), 
                comment=form.cleaned_data["comment"]
            )
            
    # Obtener comentarios y paginar manualmente (grupos de 10)
    comments_list = Discuss.objects.all().order_by("-date")
    paginated_comments = {}
    
    if len(comments_list) > 10:
        for i, start_idx in enumerate(range(0, len(comments_list), 10)):
            paginated_comments[str(i)] = comments_list[start_idx : start_idx + 10]
    else:
        paginated_comments[0] = comments_list

    return render(request, 'discuss.html', {
        "comments": paginated_comments, 
        "form": DiscussForm()
    })

# ==============================================================================
# 2. DASHBOARD Y ANÁLISIS CORE
# ==============================================================================

def rubric_creator(request):
    user = str(identify_user_type(request))
    return render(request, user + '/rubric-creator.html')

def upload_personalized(request, skill_points=None):
    user = str(identify_user_type(request))
    return render(request, user + '/rubric-uploader.html')

def compare_uploader(request):
    user = str(identify_user_type(request))
    return render(request, user + '/compare-uploader.html')

def show_dashboard(request, skill_points=None):
    user = str(identify_user_type(request))
    
    if request.method == 'POST':
        # 1. Decodificar rúbrica personalizada de la URL
        url_code = request.path.split('/')[-1]
        numbers = base32_to_str(url_code) if url_code else ''
        skill_rubric = generate_rubric(numbers)
        
        # 2. Ejecutar análisis (Normal, Comparación o Batch)
        # Nota: build_dictionary_with_automatic_analysis ya maneja la lógica de Snap! y errores
        d = build_dictionary_with_automatic_analysis(request, skill_rubric)
        
        if request.POST.get('dashboard_mode') == 'Comparison':
            return render(request, user + '/dashboard-compare.html', d)
        
        # Normalizar respuesta (a veces devuelve dict indexado {0: {...}})
        if isinstance(d, dict) and 0 in d: 
            d = d[0]
        
        # 3. Guardar en sesión para recargas (F5)
        request.session['last_analysis_data'] = d
        request.session['last_dashboard_mode'] = d.get("dashboard_mode")

        # 4. Manejo de casos especiales
        if d.get('multiproject'):
            context = { 'ETA': calc_eta(d.get('num_projects', 0)) }
            return render(request, user + '/dashboard-bulk-landing.html', context)
        
        # 5. Manejo de Errores
        error_type = d.get('Error')
        if error_type and error_type != 'None':
            if error_type == 'analyzing': 
                return render(request, 'error/analyzing.html')
            elif error_type in ['MultiValueDict', 'id_error', 'no_exists']:
                # Muestra el error en la página principal
                return render(request, user + '/main.html', {error_type: True})

        # 6. Renderizar Dashboard según modo
        mode = d.get("dashboard_mode")
        template_map = {
            'Personalized': 'dashboard-personal.html',
            'Recommender': 'dashboard-recommender.html'
        }
        template = template_map.get(mode, 'dashboard-default.html')
        
        return render(request, user + '/' + template, d)

    else:
        # GET Request: Cargar último análisis de la sesión
        d = request.session.get('last_analysis_data')
        if not d: 
            return redirect('/')
        
        dashboard_mode = request.session.get('last_dashboard_mode')
        if dashboard_mode == 'Comparison': 
            return render(request, user + '/dashboard-compare.html', d)
        
        mode = d.get("dashboard_mode")
        template_map = {
            'Personalized': 'dashboard-personal.html',
            'Recommender': 'dashboard-recommender.html'
        }
        template = template_map.get(mode, 'dashboard-default.html')
        
        return render(request, user + '/' + template, d)

def build_dictionary_with_automatic_analysis(request, skill_points: dict) -> dict:
    dict_metrics = {}
    project_counter = 0
    dashboard_mode = 'Default'

    if request.method == 'POST':
        dashboard_mode = request.POST.get('dashboard_mode', 'Default')

    if dashboard_mode == 'Comparison':
        dict_metrics = _make_compare(request, skill_points)
    else:
        if "_upload" in request.POST:
            try:
                zip_file = request.FILES['zipFile']
                dict_metrics[project_counter] = analysis_by_upload(request, skill_points, zip_file)
            except MultiValueDictKeyError:
                dict_metrics[project_counter] = {'Error': 'MultiValueDict'}
                
        elif '_url_recom' in request.POST:
            url = request.POST.get('urlProject_recom',)
            if url:
                dict_metrics[project_counter] = analysis_by_url(request, url, skill_points)
            else:
                dict_metrics[project_counter] =  {'Error': 'MultiValueDict'}
                
        # --- BLOQUE MODIFICADO PARA SNAP! ---
        elif '_url' in request.POST:
            # Saltamos la validación estricta de UrlForm que fallaba con Snap
            url = request.POST.get('urlProject')
            
            if url:
                url = url.strip()
                try:
                    # Usamos analysis_by_url directamente.
                    # Esta función se encarga de descargar y analizar.
                    dict_metrics[project_counter] = analysis_by_url(request, url, skill_points)
                except Exception as e:
                    print(f"Error en Dashboard con URL {url}: {e}")
                    dict_metrics[project_counter] = {'Error': 'Error analyzing project', 'details': str(e)}
            else:
                dict_metrics[project_counter] =  {'Error': 'MultiValueDict'}
        # ------------------------------------
                
        elif '_urls' in request.POST:
            # (Código legacy para batch antiguo vía Celery)
            try:
                projects_file = request.FILES['urlsFile']
                if not projects_file.content_type.endswith('zip'): 
                    projects = projects_file.readlines()
                    num_projects = len(projects)
                else:
                    projects_path = extract_batch_projects(projects_file)
                    num_projects = calc_num_projects(projects_path)
                    projects = projects_path
                
                request_data = {
                    'LANGUAGE_CODE': request.LANGUAGE_CODE,
                    'POST': {
                        'urlsFile': projects,
                        'dashboard_mode': 'Default', 
                        'email': request.POST.get('batch-email', '')       
                    }
                }
                init_batch.delay(request_data, skill_points) 
                dict_metrics[project_counter] = {
                    'multiproject': True,
                    'num_projects': num_projects
                }
            except Exception as e:
                dict_metrics[project_counter] = {'Error': 'MultiValueDict'}

    return dict_metrics

def download_certificate(request):
    """ Genera y descarga el certificado en PDF del análisis """
    if request.method != "POST":
        return HttpResponseRedirect('/')

    # 1. Preparar datos
    filename = request.POST.get("filename", "")
    # Normalizar nombre (quitar tildes y caracteres raros para el sistema de archivos)
    filename_ascii = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('utf-8')
    
    clean_name = clean_filename(filename_ascii)
    latex_name = escape_latex_for_url(clean_name)
    
    level = request.POST.get("level", "Basic")
    lang_code = request.LANGUAGE_CODE if is_supported_language(request.LANGUAGE_CODE) else 'en'
    
    # 2. Generar PDF (Llama a app.pyploma)
    generate_certificate(latex_name, level, lang_code)
    
    # 3. Servir el archivo
    # Asumimos estructura: app/views.py -> (parent) -> app/certificate/output.pdf
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pdf_path = os.path.join(base_dir, "app", "certificate", "output.pdf")
    
    if os.path.exists(pdf_path):
        with open(pdf_path, 'rb') as pdf_file:
            response = HttpResponse(pdf_file.read(), content_type='application/pdf')
            # Nombre de descarga limpio
            out_name = os.path.basename(clean_name).replace('.sb3', '') + ".pdf"
            response['Content-Disposition'] = f'attachment; filename="{smart_str(out_name)}"'
            return response
            
    return HttpResponseRedirect('/')

# ==============================================================================
# 3. API ENDPOINTS
# ==============================================================================

@csrf_exempt
def get_recommender(request, skill_points=None):
    """
    Endpoint API para obtener recomendaciones del sistema (usado por AJAX).
    """
    if request.method == 'POST':
        # Generamos rúbrica por defecto
        skill_rubric = generate_rubric('')
        
        # Ejecutamos análisis (la función lee los datos directamente del request.POST)
        d = build_dictionary_with_automatic_analysis(request, skill_rubric)
        
        # Normalizamos la respuesta si viene indexada
        if isinstance(d, dict) and 0 in d: 
            d = d[0]
            
        # Verificación de errores
        if d.get('Error') and d.get('Error') != 'None':
            return JsonResponse({'error': d['Error']}, status=400)
        
        # Retorno seguro de datos
        return JsonResponse(d.get('recomenderSystem', {}))        
    else:
        return HttpResponseRedirect('/')

def get_analysis_d(request, skill_points=None):
    if request.method == 'POST':
        url = request.path.split('/')[-1]
        numbers = base32_to_str(url) if url else ''
        skill_rubric = generate_rubric(numbers)
        path_original_project = request.session.get('current_project_path', None)
        if path_original_project != None:
            json_scratch_original = load_json_project(path_original_project)
        d = build_dictionary_with_automatic_analysis(request, skill_rubric) 
        path_compare_project = request.session.get('current_project_path', None)
        if path_compare_project != None:
            json_scratch_compare = load_json_project(path_compare_project)
        dict_scratch_golfing = ScratchGolfing(json_scratch_original, json_scratch_compare).finalize()
        dict_scratch_golfing = dict_scratch_golfing['result']['scratch_golfing']
        user = str(identify_user_type(request))
        
        # Corrección segura para obtener los datos si están anidados o no
        project_data = d[0] if isinstance(d, dict) and 0 in d else d
        
        # Recuperamos datos anidados para la comparación
        dict_mastery = project_data.get('mastery_vanilla', {})
        dict_dups = project_data.get('duplicateScript', {})
        dict_dead_code = project_data.get('deadCode', {})
        dict_sprite = project_data.get('spriteNaming', {})
        dict_backdrop = project_data.get('backdropNaming', {})
        
        # Limpieza segura
        if 'duplicateScript' in dict_dups: del dict_dups['duplicateScript']
        if 'deadCode' in dict_dead_code: del dict_dead_code['deadCode']
        if 'spriteNaming' in dict_sprite: del dict_sprite['spriteNaming']
        if 'backdropNaming' in dict_backdrop: del dict_backdrop['backdropNaming']
            
        context = {
            'mastery': dict_mastery,
            'duplicateScript': dict_dups,
            'deadCode': dict_dead_code,
            'spriteNaming': dict_sprite,
            'backdropNaming': dict_backdrop,
            'scratchGolfing': dict_scratch_golfing,
        }
        return JsonResponse(context)

def plugin(request, urlProject):
    """ Vista para extensiones/plugins externos que analizan una URL """
    id_project = return_scratch_project_identifier(urlProject)
    d = generator_dic(request, id_project)
    
    # Gestión de errores
    error = d.get('Error')
    if error == 'analyzing': return render(request, 'error/analyzing.html')
    elif error in ['MultiValueDict', 'id_error', 'no_exists']:
        return render(request, 'main/main.html', {error: True})
    
    # Determinar nivel según puntos
    user = "main"
    points = d.get("mastery", {}).get("points", 0)
    # Aseguramos que sea un entero (a veces viene como lista [puntos, max])
    if isinstance(points, list): 
        points = points[0]
        
    if points >= 15: 
        return render(request, user + '/dashboard-master.html', d)
    elif points > 7: 
        return render(request, user + '/dashboard-developing.html', d)
    else: 
        return render(request, user + '/dashboard-basic.html', d) 

def blocks(request):
    """ Devuelve las traducciones de los bloques en JSONP """
    callback = request.GET.get('callback')
    data = json.dumps({'Accept-Language': str(request.LANGUAGE_CODE)})
    
    if callback:
        response_data = f'{callback}({data})'
    else:
        response_data = data
        
    return HttpResponse(response_data, content_type="application/json")

def blocks_v3(request):
    return render(request, 'learn/blocks_v3.html')

def search_email(request):
    if request.is_ajax():
        email = request.GET.get('email')
        if Organization.objects.filter(email=email).exists():
            return JsonResponse({"exist": "yes"})
    return JsonResponse({"exist": "no"})

def search_username(request):
    if request.is_ajax():
        username = request.GET.get('username')
        if Organization.objects.filter(username=username).exists():
            return JsonResponse({"exist": "yes"})
    return JsonResponse({"exist": "no"})

def search_hashkey(request):
    if request.is_ajax():
        hashkey = request.GET.get('hashkey')
        # Nota: Aquí la lógica original devolvía "yes" si NO existe el usuario (validando el hash)
        # Ojo: OrganizationHash son los hashes válidos para registrarse
        if not OrganizationHash.objects.filter(hashkey=hashkey).exists():
             # Si NO existe en la tabla de hashes permitidos, es inválido (o ya usado)
             # La lógica del frontend suele esperar "exist: yes" para mostrar error
             return JsonResponse({"exist": "yes"}) 
    return JsonResponse({"exist": "no"})

# ==============================================================================
# 4. BATCH MODE & CSVS
# ==============================================================================

def batch_mode_view(request):
    """
    Renderiza la página dedicada para el modo Batch (GET).
    """
    user = None
    username = None
    flag_user = False
    
    if request.user.is_authenticated:
        username = request.user.username
        user = identify_user_type(request)
        flag_user = True
    
    return render(request, 'main/batch_mode.html', {
        'username': username,
        'user': user,
        'flagUser': flag_user
    })

def batch_analyze(request):
    """
    Procesa la subida del archivo Batch (POST).
    """
    if request.method == 'POST' and request.FILES.get('batchFile'):
        uploaded_file = request.FILES['batchFile']
        filename = uploaded_file.name.lower()
        
        # Diccionario para acumular resultados
        batch_results = {}
        project_counter = 0 
        skill_rubric = generate_rubric('') 

        try:
            # CASO A: ZIP
            if filename.endswith('.zip'):
                with zipfile.ZipFile(uploaded_file, 'r') as z:
                    for name in z.namelist():
                        if not name.startswith('__') and (name.lower().endswith('.xml') or name.lower().endswith('.sb3')):
                            try:
                                file_content = z.read(name)
                                temp_file = SimpleUploadedFile(name, file_content)
                                analysis = analysis_by_upload(request, skill_rubric, temp_file)
                                analysis['filename'] = name
                                analysis['url'] = "ZIP Upload"
                                batch_results[project_counter] = analysis
                                project_counter += 1
                            except Exception as e:
                                print(f"Error analizando {name}: {e}")

            # CASO B: TXT (MODIFICADO PARA SNAP!)
            elif filename.endswith('.txt'):
                content = uploaded_file.read().decode('utf-8')
                urls = content.splitlines()
                
                for url in urls:
                    url = url.strip()
                    if url:
                        try:
                            # Llamamos a analysis_by_url para cada línea
                            analysis = analysis_by_url(request, url, skill_rubric)
                            
                            if not analysis:
                                analysis = {'Error': 'No data returned', 'mastery': {'points': 0}}
                            
                            # Si devuelve error interno
                            if analysis.get('Error'):
                                print(f"Error reportado para {url}: {analysis.get('Error')}")

                            analysis['filename'] = url
                            analysis['url'] = url
                            batch_results[project_counter] = analysis
                            project_counter += 1
                        except Exception as e:
                            print(f"Excepción analizando {url}: {e}")
                            batch_results[project_counter] = {
                                'filename': url, 
                                'url': url,
                                'Error': f'Exception: {str(e)}',
                                'mastery': {'points': 0}
                            }
                            project_counter += 1
            
            else:
                 return HttpResponse("Formato no soportado. Por favor sube un .zip o un .txt", status=400)

        except Exception as e:
            return HttpResponse(f"Error procesando el archivo: {e}", status=400)

        # Generar CSVs
        if batch_results:
            try:
                batch_id = batch_utils.create_csv(request, batch_results)
                batch_obj = BatchCSV.objects.get(id=batch_id)
                zip_filepath = batch_obj.filepath

                if os.path.exists(zip_filepath):
                    zip_file = open(zip_filepath, 'rb')
                    response = FileResponse(zip_file, content_type='application/zip')
                    response['Content-Disposition'] = f'attachment; filename="DrSnap_Batch_{batch_id}.zip"'
                    return response
                else:
                    return HttpResponse("Error: El archivo ZIP no se encuentra.", status=500)

            except Exception as e:
                import traceback
                traceback.print_exc()
                return HttpResponse(f"Error generando los CSVs detallados: {e}", status=500)
        else:
             return HttpResponse("No se encontraron proyectos válidos para analizar.", status=400)

    return HttpResponseRedirect('/')

def batch(request, csv_identifier):
    """
    Muestra el dashboard de resultados para un lote (Batch) específico guardado previamente.
    """
    user = str(identify_user_type(request))
    csv_obj = get_object_or_404(BatchCSV, id=csv_identifier)
    
    # Construcción del resumen de estadísticas del lote
    summary = {
        'num_projects': csv_obj.num_projects,
        'Mastery': csv_obj.mastery,
        'Points': [csv_obj.points, csv_obj.max_points],
        
        # Competencias (Tuplas: [Puntos obtenidos, Puntos máximos])
        'Abstraction': [csv_obj.abstraction, csv_obj.max_abstraction],
        'Parallelism': [csv_obj.parallelization, csv_obj.max_parallelization],
        'Logic': [csv_obj.logic, csv_obj.max_logic],
        'Synchronization': [csv_obj.synchronization, csv_obj.max_synchronization],
        'Flow control': [csv_obj.flowControl, csv_obj.max_flowControl],
        'User interactivity': [csv_obj.userInteractivity, csv_obj.max_userInteractivity],
        'Data representation': [csv_obj.data, csv_obj.max_data],
        'Math operators': [csv_obj.math_operators, csv_obj.max_math_operators],
        'Motion operators': [csv_obj.motion_operators, csv_obj.max_motion_operators],
    }
    
    context = { 'summary': summary, 'csv_filepath': csv_obj.filepath }
    return render(request, user + '/dashboard-bulk.html', context)

def analyze_csv(request):
    """
    (Legacy) Sube un archivo CSV y lo guarda en el servidor.
    Nota: La funcionalidad real de análisis ahora se hace en 'batch_analyze'.
    """
    if request.method == 'POST' and "_upload" in request.POST:
        file = request.FILES.get('csvFile')
        if file:
            file_name = f"{request.user.username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            dir_csvs = os.path.join(settings.BASE_DIR, "csvs", file_name)
            
            # Asegurar que el directorio existe
            os.makedirs(os.path.dirname(dir_csvs), exist_ok=True)
            
            with open(dir_csvs, 'wb+') as destination:
                for chunk in file.chunks():
                    destination.write(chunk)
            
            return HttpResponseRedirect('/')
            
    return HttpResponseRedirect("/organization")

# ==============================================================================
# 5. GESTIÓN DE USUARIOS
# ==============================================================================

def sign_up_organization(request):
    # Inicializamos flags para la plantilla (manteniendo compatibilidad con HTML antiguo)
    context = {
        'flagName': 0, 'flagEmail': 0, 'flagHash': 0, 
        'flagForm': 0, 'flagOrganization': 1
    }
    
    if request.method == 'POST':
        form = OrganizationForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']
            hashkey = form.cleaned_data['hashkey']
            
            # Validaciones de existencia
            if User.objects.filter(username=username).exists():
                context['flagName'] = 1
                return render(request, 'error/sign-up.html', context)
            
            if User.objects.filter(email=email).exists():
                context['flagEmail'] = 1
                return render(request, 'error/sign-up.html', context)
            
            # Validación de Hashkey (Invitación)
            try:
                org_hash = OrganizationHash.objects.get(hashkey=hashkey)
            except OrganizationHash.DoesNotExist:
                context['flagHash'] = 1
                return render(request, 'error/sign-up.html', context)

            # Creación de usuario
            Organization.objects.create_user(
                username=username, email=email, password=password, hashkey=hashkey
            )
            org_hash.delete() # Consumir el hash
            
            # Enviar email de bienvenida
            try:
                user = Organization.objects.get(email=email)
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                token = default_token_generator.make_token(user)
                body = render_to_string("organization/email-sign-up.html", {
                    'email': email, 'uid': uid, 'token': token
                })
                EmailMessage("Welcome to Dr. Scratch", body, "no-reply@drscratch.org", [email]).send()
            except Exception as e:
                logger.error(f"Error sending welcome email: {e}")

            # Login automático y redirección
            user = authenticate(username=username, password=password)
            login(request, user)
            return HttpResponseRedirect('/organization/' + user.username)
            
        else:
            context['flagForm'] = 1
            return render(request, 'error/sign-up.html', context)
            
    elif request.method == 'GET':
        if request.user.is_authenticated:
            return HttpResponseRedirect('/organization/' + request.user.username)
        return render(request, 'organization/organization.html')

def login_organization(request):
    if request.method == 'POST':
        form = LoginOrganizationForm(request.POST)
        if form.is_valid():
            user = authenticate(
                username=form.cleaned_data['username'], 
                password=form.cleaned_data['password']
            )
            if user is not None and user.is_active:
                login(request, user)
                return HttpResponseRedirect('/organization/' + user.username)
            
            # Fallo de autenticación
            return render(request, 'sign-password/user-doesnt-exist.html', 
                          {'flag': True, 'flagOrganization': 1})
                          
    return HttpResponseRedirect("/")

def logout_organization(request):
    logout(request)
    return HttpResponseRedirect('/')

def organization(request, name):
    """ Perfil de la organización """
    if request.method == 'GET':
        if request.user.is_authenticated:
            username = request.user.username
            if username == name:
                try:
                    user_obj = Organization.objects.get(username=username)
                    return render(request, 'organization/main.html', 
                                  {'username': username, "img": str(user_obj.img)})
                except Organization.DoesNotExist:
                    logout(request)
                    return HttpResponseRedirect("/organization")
            else:
                return render(request, 'organization/organization.html')
        return render(request, 'organization/organization.html')
    return HttpResponseRedirect("/")

def coder_hash(request):
    """ Vista para el hash de invitación de coders (si aplica) """
    if request.method == "POST":
        form = CoderHashForm(request.POST)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect('/coder_hash')
    return render(request, 'coder/coder-hash.html')

def sign_up_coder(request):
    context = {
        'flagName': 0, 'flagEmail': 0, 'flagHash': 0, 
        'flagForm': 0, 'flagCoder': 1,
        'flagWrongEmail': 0, 'flagWrongPassword': 0
    }
    
    if request.method == 'POST':
        form = CoderForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            username = data['username']
            email = data['email']
            password = data['password']
            
            # Validaciones
            if User.objects.filter(username=username).exists():
                context['flagName'] = 1
                return render(request, 'error/sign-up.html', context)
            
            if User.objects.filter(email=email).exists():
                context['flagEmail'] = 1
                return render(request, 'error/sign-up.html', context)
            
            if email != data['email_confirm']:
                context['flagWrongEmail'] = 1
                return render(request, 'error/sign-up.html', context)
            
            if password != data['password_confirm']:
                context['flagWrongPassword'] = 1
                return render(request, 'error/sign-up.html', context)
            
            # Creación del usuario
            coder = Coder.objects.create_user(
                username=username, email=email, password=password,
                birthmonth=data['birthmonth'], birthyear=data['birthyear'],
                gender=data['gender'], country=data['country']
            )
            
            # Login automático
            user = authenticate(username=username, password=password)
            login(request, user)
            return HttpResponseRedirect('/coder/' + coder.username)
        else:
            context['flagForm'] = 1
            return render(request, 'error/sign-up.html', context)
            
    elif request.method == 'GET':
        if request.user.is_authenticated:
            return HttpResponseRedirect('/coder/' + request.user.username)
        return render(request, 'main/main.html')

def login_coder(request):
    if request.method == 'POST':
        form = LoginOrganizationForm(request.POST)
        if form.is_valid():
            user = authenticate(
                username=form.cleaned_data['username'], 
                password=form.cleaned_data['password']
            )
            if user is not None and user.is_active:
                login(request, user)
                return HttpResponseRedirect('/coder/' + user.username)
            
            # Fallo de autenticación
            return render(request, 'sign-password/user-doesnt-exist.html', 
                          {'flag': True, 'flagCoder': 1})
    return HttpResponseRedirect("/")

def logout_coder(request):
    logout(request)
    return HttpResponseRedirect('/')

def coder(request, name):
    """ Perfil del Coder """
    if request.user.is_authenticated and request.user.username == name:
        try:
            user_obj = Coder.objects.get(username=name)
            return render(request, 'coder/main.html', 
                          {'username': name, "img": str(user_obj.img)})
        except Coder.DoesNotExist:
            logout(request)
    return HttpResponseRedirect("/")

def change_pwd(request):
    """ Gestiona la solicitud de cambio de contraseña (envío de email) """
    if request.method == 'POST':
        email_addr = request.POST.get('email')
        user = None
        
        # Buscamos el usuario en ambas tablas
        try:
            if Organization.objects.filter(email=email_addr).exists():
                user = Organization.objects.get(email=email_addr)
            elif Coder.objects.filter(email=email_addr).exists():
                user = Coder.objects.get(email=email_addr)
            
            if user:
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                token = default_token_generator.make_token(user)
                
                context = {
                    'email': email_addr, 
                    'uid': uid, 
                    'token': token, 
                    'id': user.username
                }
                body = render_to_string("sign-password/email-reset-pwd.html", context)
                
                subject = "Dr. Scratch: Did you forget your password?"
                sender = "no-reply@drscratch.org"
                EmailMessage(subject, body, sender, [email_addr]).send()
                
                return render(request, 'sign-password/email-sended.html')
        except Exception as e:
            logger.error(f"Error in change_pwd: {e}")
            
        # Si no se encuentra o falla
        return render(request, 'sign-password/user-doesnt-exist.html')
        
    return render(request, 'sign-password/password.html')

def reset_password_confirm(request, uidb64=None, token=None, *arg, **kwargs):
    """ Procesa el cambio de contraseña una vez pulsado el link del email """
    UserModel = get_user_model()
    user = None
    page = 'main'

    try:
        uid = urlsafe_base64_decode(uidb64)
        # Determinamos si es Organización o Coder para la redirección final
        if Organization.objects.filter(pk=uid).exists():
            user = Organization.objects.get(pk=uid)
            page = 'organization'
        elif Coder.objects.filter(pk=uid).exists():
            user = Coder.objects.get(pk=uid)
            page = 'coder'
    except (TypeError, ValueError, OverflowError, UserModel.DoesNotExist):
        user = None

    if request.method == "POST":
        if user and default_token_generator.check_token(user, token):
            pwd = request.POST.get('password')
            confirm = request.POST.get('confirm')
            
            if pwd and pwd == confirm:
                user.set_password(pwd)
                user.save()
                # Es necesario reloguear al usuario tras cambiar password
                logout(request)
                new_user = authenticate(username=user.username, password=pwd)
                if new_user:
                    login(request, new_user)
                return HttpResponseRedirect(f'/{page}/{user.username}')
            else:
                return render(request, 'sign-password/new-password.html', {'flag_error': True})
    
    # GET Request: Verificar token válido antes de mostrar formulario
    if user and default_token_generator.check_token(user, token):
        return render(request, 'sign-password/new-password.html')
    else:
        return render(request, f'{page}/main.html')

# ==============================================================================
# 6. ESTADÍSTICAS Y DESCARGAS
# ==============================================================================

def stats(request, username):
    """ 
    Genera las estadísticas visuales (Gráficos) para Organizaciones y Coders.
    Optimizado para reducir consultas a la base de datos.
    """
    # 1. Identificar usuario y obtener sus archivos
    if Organization.objects.filter(username=username).exists():
        user = Organization.objects.get(username=username)
        files = File.objects.filter(organization=username)
        page = 'organization'
    elif Coder.objects.filter(username=username).exists():
        user = Coder.objects.get(username=username)
        files = File.objects.filter(coder=username)
        page = 'coder'
    else:
        # Si el usuario no existe en ninguna tabla específica
        return HttpResponseRedirect("/")

    # 2. Calcular puntuación diaria (Gráfico de línea)
    date_joined = user.date_joined.date()
    end_date = datetime.today().date()
    date_list = date_range(date_joined, end_date)
    
    mydates = []
    daily_score = []
    
    for n in date_list:
        mydates.append(n.strftime("%d/%m"))
        # Filtramos por fecha exacta (usando __date para comparar con objeto date)
        points = files.filter(time__date=n).aggregate(Avg("score"))["score__avg"]
        daily_score.append(int(points) if points is not None else 0)

    # 3. Calcular métricas de habilidades (Gráfico de araña)
    # Inicializamos a 0
    skill_metrics = {
        "Parallelism": 0, "abstraction": 0, "logic": 0, 
        "synchronization": 0, "flowControl": 0, 
        "userInteractivity": 0, "dataRepresentation": 0
    }
    
    if files.exists():
        # Agregamos todas las medias en una sola consulta
        avgs = files.aggregate(
            Avg("Parallelism"), Avg("abstraction"), Avg("logic"), 
            Avg("synchronization"), Avg("flowControl"), 
            Avg("userInteractivity"), Avg("dataRepresentation")
        )
        # Limpiamos los resultados (None -> 0, float -> int)
        for key, val in avgs.items():
            clean_key = key.replace("__avg", "") # ej: Parallelism__avg -> Parallelism
            skill_metrics[clean_key] = int(val) if val else 0

    # 4. Calcular métricas globales de Code Smells (Comparativa global)
    # Nota: Compara contra TODOS los archivos del sistema, no solo los del usuario
    all_files = File.objects.all()
    global_smells = all_files.aggregate(
        Avg("deadCode"), Avg("duplicateScript"), 
        Avg("spriteNaming"), Avg("initialization")
    )
    
    code_smell_rate = {
        "deadCode": int(global_smells["deadCode__avg"] or 0),
        "duplicateScript": int(global_smells["duplicateScript__avg"] or 0),
        "spriteNaming": int(global_smells["spriteNaming__avg"] or 0),
        "initialization": int(global_smells["initialization__avg"] or 0)
    }

    dic = {
        "date": mydates, 
        "username": username, 
        "img": user.img, 
        "daily_score": daily_score,
        "skillRate": skill_metrics,
        "codeSmellRate": code_smell_rate
    }
    
    return render(request, page + '/stats.html', dic)

def downloads(request, username, filename=""):
    """
    Permite ver y descargar el historial de CSVs generados.
    """
    # Identificar usuario
    if Organization.objects.filter(username=username).exists():
        user = Organization.objects.get(username=username)
        csv_list = CSVs.objects.filter(organization=username).order_by('-date')
        page = 'organization'
    elif Coder.objects.filter(username=username).exists():
        user = Coder.objects.get(username=username)
        csv_list = CSVs.objects.filter(coder=username).order_by('-date')
        page = 'coder'
    else:
        return HttpResponseRedirect("/")

    # Paginación manual para la plantilla (grupos de 10)
    paginated_csv = {}
    csv_len = len(csv_list)
    
    if csv_len > 10:
        for i, start_idx in enumerate(range(0, csv_len, 10)):
            paginated_csv[str(i)] = csv_list[start_idx : start_idx + 10]
        context = {"username": username, "img": user.img, "csv": paginated_csv, "flag": 1}
    else:
        context = {"username": username, "img": user.img, "csv": csv_list, "flag": 0}

    # Procesar descarga si es POST
    if request.method == "POST":
        filename = request.POST.get("csv", "")
        safe_filename = os.path.basename(filename)
        # Usamos settings.BASE_DIR para una ruta segura
        csv_path = os.path.join(settings.BASE_DIR, "csvs", "Dr.Scratch", safe_filename)
        
        if validate_csv(csv_path):
            with open(csv_path, 'rb') as f:
                response = HttpResponse(f.read(), content_type='text/csv')
                response['Content-Disposition'] = f'attachment; filename={smart_str(safe_filename)}'
                return response
        return HttpResponse("Invalid CSV file or path", status=400)

    return render(request, page + '/downloads.html', context)

def statistics(request):
    """ Muestra estadísticas globales de la plataforma (Dr. Scratch en números) """
    start = date(2015, 8, 1)
    end = datetime.today().date()
    date_list = date_range(start, end)
    my_dates = [d.strftime("%d/%m") for d in date_list]
    
    data = {}
    try:
        # Obtenemos el último objeto Stats generado
        obj = Stats.objects.order_by("-id").first()
        
        if obj:
            data = {
                "date": my_dates,
                "dailyRate": obj.daily_score,
                "levels": { 
                    "basic": obj.basic, 
                    "development": obj.development, 
                    "master": obj.master 
                },
                "totalProjects": obj.daily_projects,
                "skillRate": { 
                    "Parallelism": obj.parallelization, "abstraction": obj.abstraction, 
                    "logic": obj.logic, "synchronization": obj.synchronization, 
                    "flowControl": obj.flow_control, "userInteractivity": obj.userInteractivity, 
                    "dataRepresentation": obj.dataRepresentation 
                },
                "codeSmellRate": { 
                    "deadCode": obj.deadCode, "duplicateScript": obj.duplicateScript, 
                    "spriteNaming": obj.spriteNaming, "initialization": obj.initialization 
                }
            }
    except Exception as e:
        logger.error(f"Error loading global statistics: {e}")
        
    return render(request, 'main/statistics.html', data)

# ==============================================================================
# 7. FUNCIONES AUXILIARES
# ==============================================================================

def base32_to_str(base32_str: str) -> str:
    try:
        value = int(base32_str, 32)
        return str(value).zfill(9)
    except (ValueError, TypeError):
        return ""

def calc_eta(num_projects: int) -> str:
    """ Calcula el tiempo estimado basado en los últimos 10 lotes """
    last_ten = BatchCSV.objects.all().order_by('-date')[:10]
    if not last_ten: return "Calculating..."
    
    try:
        # Evitamos división por cero si un batch antiguo falló y tiene 0 proyectos
        anal_time = sum(batch.task_time / batch.num_projects for batch in last_ten if batch.num_projects > 0) / 10
        mean_tm = anal_time * num_projects
        
        hours = int(mean_tm // 3600)
        minutes = int((mean_tm % 3600) // 60)
        seconds = round(mean_tm % 60, 2)
        
        return f'{hours}h: {minutes}min: {seconds}s'
    except Exception:
        return "Calculating..."

def generate_rubric(skill_points: str) -> dict:
    """ Genera la rúbrica de evaluación basada en la URL codificada """
    mastery = ['Abstraction', 'Parallelization', 'Logic', 'Synchronization', 
               'FlowControl', 'UserInteractivity', 'DataRepresentation', 
               'MathOperators', 'MotionOperators']
    skill_rubric = {}
    
    if skill_points:
        # Si vienen puntos personalizados en la URL
        for skill_name, points in zip(mastery, skill_points):
            try:
                skill_rubric[skill_name] = int(points)
            except ValueError:
                skill_rubric[skill_name] = 4
    else:
        # Valores por defecto (4 puntos por competencia)
        for skill_name in mastery:
            skill_rubric[skill_name] = 4      
    return skill_rubric

def calc_num_projects(batch_path: str) -> int:
    """ Calcula recursivamente cuántos archivos hay en el directorio temporal """
    num_projects = 0
    for root, dirs, files in os.walk(batch_path):
        # Filtramos archivos ocultos o del sistema (__MACOSX, .DS_Store)
        valid_files = [f for f in files if not f.startswith('.') and not f.startswith('__')]
        num_projects += len(valid_files)
    return num_projects
    
def extract_batch_projects(projects_file: object) -> str:
    """ 
    Extrae el ZIP subido a una carpeta temporal y devuelve la ruta.
    """
    project_name = str(uuid.uuid4())
    # Generamos un ID único con timestamp para evitar colisiones
    unique_id = '{}_{}'.format(
        project_name, 
        datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f")
    )
    
    base_dir = os.getcwd()
    extraction_path = os.path.join(base_dir, 'uploads', 'batch_mode', unique_id)
    
    # Usamos un directorio temporal del sistema para la extracción inicial
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_file_path = os.path.join(temp_dir, projects_file.name)
        
        # 1. Guardar el ZIP en disco temporalmente
        with open(temp_file_path, 'wb+') as temp_file:
            for chunk in projects_file.chunks():
                temp_file.write(chunk)
            
            # 2. Descomprimir en la ruta final ('uploads/batch_mode/ID')
            try:
                with ZipFile(temp_file, 'r') as zip_ref:
                    zip_ref.extractall(extraction_path)
            except BadZipfile:
                # Si el zip está corrupto, limpiamos y lanzamos error
                if os.path.exists(extraction_path):
                    shutil.rmtree(extraction_path)
                raise
                
    return extraction_path

def identify_user_type(request) -> str:
    """ Determina el tipo de usuario (Organization, Coder, Admin, o Main) """
    if not request.user.is_authenticated:
        return 'main'
        
    username = request.user.username
    
    # Comprobamos tablas específicas
    if Organization.objects.filter(username=username).exists():
        return 'organization'
    elif Coder.objects.filter(username=username).exists():
        return 'coder'
        
    # Comprobamos permisos de Django
    if request.user.is_superuser:
        return 'superuser'
    elif request.user.is_staff:
        return 'staff'
        
    return 'main'

def identify_admin(user_type: str) -> bool:
    """ Devuelve True si el usuario tiene privilegios de administración """
    return user_type in ['superuser', 'staff']

def escape_latex_for_url(text: str) -> str:
    """ Escapa caracteres especiales de LaTeX para generar el PDF """
    special_chars = {
        "_": r"\_",
        "&": r"\&",
        "%": r"\%",
        "{": r"\{",
        "}": r"\}"
    }
    for char, escaped in special_chars.items():
        text = text.replace(char, escaped)
    return text

def clean_filename(filename: str) -> str:
    """ Limpia nombres de archivo temporales de Scratch (ej: ;filename.sb3) """
    # Busca patrones tipo ;nombre.sb3 que a veces genera el upload
    match = re.search(r';.*.sb3', filename)
    if match:
        clean = match.group(0)
        return re.sub(';', '', clean)
    return filename

def is_supported_language(language_code: str) -> bool:
    """ Verifica si el idioma está soportado para el certificado """
    return language_code in supported_languages

def validate_csv(csv_file_path: str) -> bool:
    """ Valida que el archivo exista y sea un CSV real """
    return os.path.isfile(csv_file_path) and csv_file_path.endswith('.csv')

def date_range(start, end):
    """ Genera una lista de fechas entre start y end """
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]

def load_json_project(path_projectsb3):
    try:
        zip_file = ZipFile(path_projectsb3, "r")
        json_project = json.loads(zip_file.open("project.json").read())
        return json_project
    except BadZipfile:
        print('Bad zipfile')


"""
def organization_hash(request):
    if request.method == "POST":
        form = OrganizationHashForm(request.POST)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect('/organization_hash')
    elif request.method == 'GET':
        return render(request, 'organization/organization-hash.html') 
    else:
        return HttpResponseRedirect('/')
"""
#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import os
import ast
import json
import uuid
import requests
import tempfile
import csv
from datetime import datetime, timedelta, date
import traceback
import re
import zipfile
from zipfile import ZipFile, BadZipfile
import pickle
import shutil
import unicodedata
import logging
import coloredlogs

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
from django.utils.html import strip_tags
from django.core.files.uploadedfile import SimpleUploadedFile

# App imports
from .models import BatchCSV, File, CSVs, Organization, OrganizationHash, Coder, Discuss, Stats
from app import org
from app.forms import UrlForm, OrganizationForm, OrganizationHashForm, LoginOrganizationForm, CoderForm, DiscussForm
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

import app.consts_drscratch as consts
from app.scratchclient import ScratchSession
from app.pyploma import generate_certificate
from app.exception import DrScratchException

# Hairball / Logic imports
from app.hairball3.mastery import Mastery
from app.hairball3.spriteNaming import SpriteNaming
from app.hairball3.backdropNaming import BackdropNaming
from app.hairball3.duplicateScripts import DuplicateScripts
from app.hairball3.deadCode import DeadCode
from app.hairball3.refactor import RefactorDuplicate
from app.hairball3.comparsionMode import ComparsionMode
from app.hairball3.scratchGolfing import ScratchGolfing
from app.hairball3.block_sprite_usage import Block_Sprite_Usage

# Celery & Analyzer
from .tasks import init_batch
from .analyzer import analyze_project, generator_dic, return_scratch_project_identifier, send_request_getsb3, _make_compare, analysis_by_upload, analysis_by_url
from .batch import skills_translation
from . import batch as batch_utils 

from .recomender import RecomenderSystem

logger = logging.getLogger(__name__)
coloredlogs.install(level='DEBUG', logger=logger)
supported_languages = ['es', 'ca', 'gl', 'pt']

# ==============================================================================
# VISTAS PRINCIPALES
# ==============================================================================

def main(request):
    user = None
    if request.user.is_authenticated:
        user_name = request.user.username
        user_type = identify_user_type(request)
        is_admin = identify_admin(user_type)
        if (is_admin):
            return render(request, 'main/main.html', {'username': user_name})
        else:
            if user_type == 'coder':
                user = Coder.objects.get(username=user_name)
            elif user_type == 'organization':
                user = Organization.objects.get(username=user_name)
            return render(request, user_type + '/main.html', {'username': user_name, "img": str(user.img)})
    else:
        return render(request, 'main/main.html', {'username': None})

def contest(request):
    return render(request, 'contest.html', {})

def collaborators(request):
    return render(request, 'main/collaborators.html')
    
def rubric_creator(request):
    user = str(identify_user_type(request))
    return render(request, user + '/rubric-creator.html')

def upload_personalized(request, skill_points=None):
    user = str(identify_user_type(request))
    return render(request, user + '/rubric-uploader.html')

def compare_uploader(request):
    user = str(identify_user_type(request))
    return render(request, user + '/compare-uploader.html')

def base32_to_str(base32_str: str) -> str:
    value = int(base32_str, 32)
    return str(value).zfill(9)

def calc_eta(num_projects: int) -> str:
    last_ten = BatchCSV.objects.all().order_by('-date')[:10]
    if not last_ten: return "Calculating..."
    anal_time = sum(batch.task_time/batch.num_projects for batch in last_ten)/10
    mean_tm = anal_time * num_projects
    eta_h = mean_tm // 3600
    eta_m = (mean_tm % 3600) // 60
    eta_s = (mean_tm % 60)
    return f'{int(eta_h)}h: {int(eta_m)}min: {round(eta_s,2)}s'
 
def show_dashboard(request, skill_points=None):
    if request.method == 'POST':
        url = request.path.split('/')[-1]
        numbers = base32_to_str(url) if url else ''
        skill_rubric = generate_rubric(numbers)
        user = str(identify_user_type(request))
        
        # ### INICIO MODIFICACIÓN DEBUG ###
        # Esto te imprimirá en la consola negra (terminal) por qué falla la URL
        if "_url" in request.POST:
            print("--- DEBUG: Comprobando URL ---")
            debug_form = UrlForm(request.POST)
            if not debug_form.is_valid():
                print("❌ ERROR DE VALIDACIÓN:")
                print(debug_form.errors)
            else:
                print("✅ Formulario válido. URL:", debug_form.cleaned_data.get('urlProject'))
        # ### FIN MODIFICACIÓN DEBUG ###

        if request.POST.get('dashboard_mode') == 'Comparison':
            d = build_dictionary_with_automatic_analysis(request, skill_rubric)
            return render(request, user + '/dashboard-compare.html', d)   
        
        d = build_dictionary_with_automatic_analysis(request, skill_rubric)
        if isinstance(d, dict) and 0 in d: d = d[0] # Normalizar si viene en dict indexado
        
        # ... resto del código igual ...
        request.session['last_analysis_data'] = d
        request.session['last_dashboard_mode'] = d.get("dashboard_mode")

        if d.get('multiproject'):
            context = { 'ETA': calc_eta(d.get('num_projects', 0)) }
            return render(request, user + '/dashboard-bulk-landing.html', context)
        
        error_type = d.get('Error')
        if error_type and error_type != 'None':
            # AQUÍ ES DONDE TE REDIRIGE ACTUALMENTE
            if error_type == 'analyzing': return render(request, 'error/analyzing.html')
            elif error_type == 'MultiValueDict': 
                print("DEBUG: Redirigiendo a main por error MultiValueDict (Formulario inválido)") # Debug extra
                return render(request, user + '/main.html', {'error': True})
            elif error_type == 'id_error': return render(request, user + '/main.html', {'id_error': True})
            elif error_type == 'no_exists': return render(request, user + '/main.html', {'no_exists': True})

        mode = d.get("dashboard_mode")
        if mode == 'Default': return render(request, user + '/dashboard-default.html', d)
        elif mode == 'Personalized': return render(request, user + '/dashboard-personal.html', d)               
        elif mode == 'Recommender': return render(request, user + '/dashboard-recommender.html', d)
        
        return render(request, user + '/dashboard-default.html', d)
    else:
        # ... código del GET (sin cambios) ...
        user = str(identify_user_type(request))
        d = request.session.get('last_analysis_data')
        if not d: return redirect('/')
        
        dashboard_mode = request.session.get('last_dashboard_mode')
        if dashboard_mode == 'Comparison': return render(request, user + '/dashboard-compare.html', d)
        
        mode = d.get("dashboard_mode")
        if mode == 'Personalized': return render(request, user + '/dashboard-personal.html', d)               
        elif mode == 'Recommender': return render(request, user + '/dashboard-recommender.html', d)
        
        return render(request, user + '/dashboard-default.html', d)

@csrf_exempt
def get_recommender(request, skill_points=None):
    if request.method == 'POST':
        url = request.POST.get('urlProject_recom') or request.POST.get('urlProject_recom')
        currType = request.POST.get('currType')
        skill_rubric = generate_rubric('')
        user = str(identify_user_type(request))
        d = build_dictionary_with_automatic_analysis(request, skill_rubric)
        d = d[0] if isinstance(d, dict) and 0 in d else d
        if d.get('Error') and d['Error'] != 'None':
            return JsonResponse({'error': d['Error']}, status=400)
        else:
            return JsonResponse(d['recomenderSystem'])        
    else:
        return HttpResponseRedirect('/')

def batch(request, csv_identifier):
    user = str(identify_user_type(request))
    csv_obj = get_object_or_404(BatchCSV, id=csv_identifier)
    csv_filepath = csv_obj.filepath
    summary = {
        'num_projects': csv_obj.num_projects,
        'Points': [csv_obj.points, csv_obj.max_points],
        'Logic': [csv_obj.logic, csv_obj.max_logic],
        'Parallelism': [csv_obj.parallelization, csv_obj.max_parallelization],
        'Data representation': [csv_obj.data, csv_obj.max_data],
        'Synchronization': [csv_obj.synchronization, csv_obj.max_synchronization],
        'User interactivity': [csv_obj.userInteractivity, csv_obj.max_userInteractivity],
        'Flow control': [csv_obj.flowControl, csv_obj.max_flowControl],
        'Abstraction': [csv_obj.abstraction, csv_obj.max_abstraction],
        'Math operators': [csv_obj.math_operators, csv_obj.max_math_operators],
        'Motion operators': [csv_obj.motion_operators, csv_obj.max_motion_operators],
        'Mastery': csv_obj.mastery
    }
    context = { 'summary': summary, 'csv_filepath': csv_filepath }
    return render(request, user + '/dashboard-bulk.html', context)

def process_contact_form(request):
    if request.method == 'POST':
        required_fields = {'contact_name': 'Please, fill your name.', 'contact_email': 'Please, fill your email.', 'contact_text': 'Please, fill the text area.'}
        for field, error_message in required_fields.items():
            if not request.POST.get(field, ''):
                messages.error(request, error_message)
                request.session['form_data'] = request.POST
                return HttpResponseRedirect('/contact')
        
        contact_name = request.POST.get('contact_name')
        contact_email = request.POST.get('contact_email')
        contact_text = request.POST.get('contact_text')
        contact_media = request.FILES.get('contact_media')

        message = f'''Name: {contact_name}, Email: {contact_email}, Text: {contact_text}'''
        subject = '[CONTACT FORM]'
        email = EmailMessage(subject, message, settings.EMAIL_HOST_USER, ['drscratch@gsyc.urjc.es'])
        if contact_media:
            email.attach(contact_media.name, contact_media.read(), contact_media.content_type)
        email.send()
        return HttpResponseRedirect('/')    
    else:
        return HttpResponse('METHOD NOT ALLOW', status=405)

def generate_rubric(skill_points: str) -> dict:
    mastery = ['Abstraction', 'Parallelization', 'Logic', 'Synchronization', 'FlowControl', 'UserInteractivity', 'DataRepresentation', 'MathOperators', 'MotionOperators']
    skill_rubric = {}
    if skill_points:
        for skill_name, points in zip(mastery, skill_points):
            skill_rubric[skill_name] = int(points)   
    else:
        for skill_name in mastery:
            skill_rubric[skill_name] = 4      
    return skill_rubric  

def calc_num_projects(batch_path: str) -> int:
    num_projects = 0
    for root, dirs, files in os.walk(batch_path):
        num_projects += len(files)
    return num_projects
    
def extract_batch_projects(projects_file: object) -> int:
    project_name = str(uuid.uuid4())
    unique_id = '{}_{}{}'.format(project_name, datetime.now().strftime("%Y_%m_%d_%H_%M_%S_"), datetime.now().microsecond)
    base_dir = os.getcwd()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_file_path = os.path.join(temp_dir, projects_file.name)
        with open(temp_file_path, 'wb+') as temp_file:
            for chunk in projects_file.chunks():
                temp_file.write(chunk)
            temp_extraction =  os.path.join(base_dir, 'uploads', 'batch_mode', unique_id)
            with ZipFile(temp_file, 'r') as zip_ref:
                zip_ref.extractall(temp_extraction)
    return temp_extraction

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

def identify_user_type(request) -> str:
    user = None
    if request.user.is_authenticated:
        username = request.user.username
        if Organization.objects.filter(username=username).exists():
            user = 'organization'
        elif Coder.objects.filter(username=username).exists():
            user = 'coder'
        elif request.user.is_staff:
            user = 'staff'
        elif request.user.is_superuser:
            user = 'superuser'
    else:
        user = 'main'
    return user

def identify_admin(user_type):
    return 1 if (user_type == 'superuser' or user_type == 'staff') else 0

def learn(request, page):
    flag_user = 1 if request.user.is_authenticated else 0
    dic = skills_translation(request)
    if page in dic: page = dic[page]
    page = '{}{}{}'.format('learn/', page, '.html')
    if request.user.is_authenticated:
        user = identify_user_type(request)
        username = request.user.username
        return render(request, page, {'flagUser': flag_user, 'user': user, 'username': username})
    else:
        return render(request, page)

def contact(request):
    return render(request, 'main/contact-form.html') 

def escape_latex_for_url(text):
    text = text.replace("_", r"\_")
    text = text.replace("&", r"\&")
    text = text.replace("%", r"\%")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    return text

def download_certificate(request):
    if request.method == "POST":
        filename = request.POST["filename"]
        filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('utf-8')
        filename = clean_filename(filename)
        filename = escape_latex_for_url(filename)
        level = request.POST["level"]
        language = request.LANGUAGE_CODE if is_supported_language(request.LANGUAGE_CODE) else 'en'
        
        generate_certificate(filename, level, language)
        path_to_file = os.path.dirname(os.path.dirname(__file__)) + "/app/certificate/output.pdf"
        
        with open(path_to_file, 'rb') as pdf_file:
           pdf_data = pdf_file.read()

        response = HttpResponse(pdf_data, content_type='application/pdf')
        try:
            file_pdf = filename.split("/")[-2] + ".pdf"
        except:
            file_pdf = filename.split(".")[0] + ".pdf"

        response['Content-Disposition'] = 'attachment; filename=%s' % file_pdf
        return response
    else:
        return HttpResponseRedirect('/')
    
def clean_filename(filename):
    pattern = r';.*.sb3'
    matches = re.findall(pattern, filename)
    if matches:
        filename = matches[0]
        filename = re.sub(';', '', filename)
    return filename

def is_supported_language(lenguage_code):
    return 1 if lenguage_code in supported_languages else 0

def search_email(request):
    if request.is_ajax():
        user = Organization.objects.filter(email=request.GET['email'])
        if user: return HttpResponse(json.dumps({"exist": "yes"}), content_type ='application/json')

def search_username(request):
    if request.is_ajax():
        user = Organization.objects.filter(username=request.GET['username'])
        if user: return HttpResponse(json.dumps({"exist": "yes"}), content_type='application/json')

def search_hashkey(request):
    if request.is_ajax():
        user = OrganizationHash.objects.filter(hashkey=request.GET['hashkey'])
        if not user: return HttpResponse(json.dumps({"exist": "yes"}), content_type='application/json')

def plugin(request, urlProject):
    user = None
    id_project = return_scratch_project_identifier(urlProject)
    d = generator_dic(request, id_project)
    if d['Error'] == 'analyzing': return render(request, 'error/analyzing.html')
    elif d['Error'] == 'MultiValueDict': return render(request, 'main/main.html', {'error': True})
    elif d['Error'] == 'id_error': return render(request, 'main/main.html', {'id_error': True})
    elif d['Error'] == 'no_exists': return render(request, 'main/main.html', {'no_exists': True})
    else:
        user = "main"
        if d["mastery"]["points"] >= 15: return render(request, user + '/dashboard-master.html', d)
        elif d["mastery"]["points"] > 7: return render(request, user + '/dashboard-developing.html', d)
        else: return render(request, user + '/dashboard-basic.html', d) 

def blocks(request):
    callback = request.GET.get('callback')
    headers = {'Accept-Language': str(request.LANGUAGE_CODE)}
    headers = json.dumps(headers)
    if callback: headers = '%s(%s)' % (callback, headers)
    return HttpResponse(headers, content_type="application/json")

def blocks_v3(request):
    return render(request, 'learn/blocks_v3.html')

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

def sign_up_organization(request):
    flag_organization = 1
    flag_hash = 0
    flag_name = 0
    flag_email = 0
    flag_form = 0
    if request.method == 'POST':
        form = OrganizationForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']
            hashkey = form.cleaned_data['hashkey']
            if User.objects.filter(username = username):
                flag_name = 1
                return render(request, 'error/sign-up.html', {'flagName':flag_name, 'flagEmail':flag_email, 'flagHash':flag_hash, 'flagForm':flag_form, 'flagOrganization':flag_organization})
            elif User.objects.filter(email = email):
                flag_email = 1
                return render(request, 'error/sign-up.html', {'flagName':flag_name, 'flagEmail':flag_email, 'flagHash':flag_hash, 'flagForm':flag_form, 'flagOrganization':flag_organization})
            if (OrganizationHash.objects.filter(hashkey = hashkey)):
                organizationHashkey = OrganizationHash.objects.get(hashkey=hashkey)
                organization = Organization.objects.create_user(username = username, email=email, password=password, hashkey=hashkey)
                organizationHashkey.delete()
                organization = authenticate(username=username, password=password)
                login(request, organization)
                return HttpResponseRedirect('/organization/' + organization.username)
            else:
                flag_hash = 1
                return render(request, 'error/sign-up.html', {'flagName':flag_name, 'flagEmail':flag_email, 'flagHash':flag_hash, 'flagForm':flag_form, 'flagOrganization':flag_organization})
        else:
            flag_form = 1
            return render(request, 'error/sign-up.html', {'flagName':flag_name, 'flagEmail':flag_email, 'flagHash':flag_hash, 'flagForm':flag_form, 'flagOrganization':flag_organization})
    elif request.method == 'GET':
        if request.user.is_authenticated:
            return HttpResponseRedirect('/organization/' + request.user.username)
        else:
            return render(request, 'organization/organization.html')

def login_organization(request):
    if request.method == 'POST':
        flag = False
        flagOrganization = 0
        form = LoginOrganizationForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            organization = authenticate(username=username, password=password)
            if organization is not None:
                if organization.is_active:
                    login(request, organization)
                    return HttpResponseRedirect('/organization/' + organization.username)
            else:
                flag = True
                flagOrganization = 1
                return render(request, 'sign-password/user-doesnt-exist.html', {'flag': flag, 'flagOrganization': flagOrganization})
    else:
        return HttpResponseRedirect("/")

def logout_organization(request):
    logout(request)
    return HttpResponseRedirect('/')

def organization(request, name):
    if request.method == 'GET':
        if request.user.is_authenticated:
            username = request.user.username
            if username == name:
                if Organization.objects.filter(username = username):
                    user = Organization.objects.get(username=username)
                    dic={'username':username, "img":str(user.img)}
                    return render(request, 'organization/main.html', dic)
                else:
                    logout(request)
                    return HttpResponseRedirect("/organization")
            else:
                return render(request, 'organization/organization.html')
        return render(request, 'organization/organization.html')
    else:
        return HttpResponseRedirect("/")

def stats(request, username):
    flag_organization = 0
    flag_coder = 0
    if Organization.objects.filter(username=username):
        flag_organization = 1
        page = 'organization'
        user = Organization.objects.get(username=username)
    elif Coder.objects.filter(username=username):
        flag_coder = 1
        page = 'coder'
        user = Coder.objects.get(username=username)

    date_joined = user.date_joined
    end = datetime.today()
    end = date(end.year, end.month, end.day)
    start = date(date_joined.year, date_joined.month,date_joined.day)
    date_list = date_range(start, end)
    daily_score = []
    mydates = []
    for n in date_list:
        mydates.append(n.strftime("%d/%m"))
        if flag_organization:
            points = File.objects.filter(organization=username).filter(time=n)
        elif flag_coder:
            points = File.objects.filter(coder=username).filter(time=n)
        points = points.aggregate(Avg("score"))["score__avg"]
        daily_score.append(points)

    for n in daily_score:
        if n is None: daily_score[daily_score.index(n)]=0

    if flag_organization: f = File.objects.filter(organization=username)
    elif flag_coder: f = File.objects.filter(coder=username)
    
    if f:
        Parallelism = int(f.aggregate(Avg("Parallelism"))["Parallelism__avg"])
        abstraction = int(f.aggregate(Avg("abstraction"))["abstraction__avg"])
        logic = int(f.aggregate(Avg("logic"))["logic__avg"])
        synchronization = int(f.aggregate(Avg("synchronization"))["synchronization__avg"])
        flowControl = int(f.aggregate(Avg("flowControl"))["flowControl__avg"])
        userInteractivity = int(f.aggregate(Avg("userInteractivity"))["userInteractivity__avg"])
        dataRepresentation = int(f.aggregate(Avg("dataRepresentation"))["dataRepresentation__avg"])

        deadCode = int(File.objects.all().aggregate(Avg("deadCode"))["deadCode__avg"])
        duplicateScript = int(File.objects.all().aggregate(Avg("duplicateScript"))["duplicateScript__avg"])
        spriteNaming = int(File.objects.all().aggregate(Avg("spriteNaming"))["spriteNaming__avg"])
        initialization = int(File.objects.all().aggregate(Avg("initialization"))["initialization__avg"])
    else:
        Parallelism,abstraction,logic=[0],[0],[0]
        synchronization,flowControl,userInteractivity=[0],[0],[0]
        dataRepresentation,deadCode,duplicateScript=[0],[0],[0]
        spriteNaming,initialization =[0],[0]

    dic = {
        "date":mydates, "username": username, "img": user.img, "daily_score":daily_score,
        "skillRate":{"Parallelism":Parallelism, "abstraction":abstraction, "logic": logic, "synchronization":synchronization, "flowControl":flowControl, "userInteractivity":userInteractivity, "dataRepresentation":dataRepresentation},
        "codeSmellRate":{"deadCode":deadCode, "duplicateScript":duplicateScript, "spriteNaming":spriteNaming, "initialization":initialization }}
    return render(request, page + '/stats.html', dic)

def account_settings(request,username):
    base_dir = os.getcwd()
    if base_dir == "/": base_dir = "/var/www/drscratchv3"
    flagOrganization = 0
    flagCoder = 0
    if Organization.objects.filter(username=username):
        page = 'organization'
        user = Organization.objects.get(username=username)
    elif Coder.objects.filter(username=username):
        page = 'coder'
        user = Coder.objects.get(username=username)

    if request.method == "POST":
        user.img = request.FILES["img"]
        os.chdir(base_dir+"/static/img")
        user.img.name = str(user.img)
        if os.path.exists(user.img.name): os.remove(user.img.name)
        os.chdir(base_dir)
        user.save()

    dic = { "username": username, "img": user.img }
    return render(request, page + '/settings.html', dic)

def downloads(request, username, filename=""):
    flagOrganization = 0
    flagCoder = 0
    if Organization.objects.filter(username=username):
        flagOrganization = 1
        user = Organization.objects.get(username=username)
    elif Coder.objects.filter(username=username):
        flagCoder = 1
        user = Coder.objects.get(username=username)

    if flagOrganization:
        csv = CSVs.objects.all().filter(organization=username)
        page = 'organization'
    elif flagCoder:
        csv = CSVs.objects.all().filter(coder=username)
        page = 'coder'

    csv_len = len(csv)
    lower = 0
    upper = 10
    list_csv = {}

    if csv_len > 10:
        for n in range(int((csv_len/10))+1):
            list_csv[str(n)]= csv[lower:upper-1]
            lower = upper
            upper = upper + 10
        dic = { "username": username, "img": user.img, "csv": list_csv, "flag": 1 }
    else:
        dic = { "username": username, "img": user.img, "csv": csv, "flag": 0 }

    if request.method == "POST":
        filename = request.POST.get("csv", "")
        safe_filename = os.path.basename(filename)
        csv_directory = os.path.join(os.path.dirname(os.path.dirname(__file__)), "csvs/Dr.Scratch")
        path_to_file = os.path.join(csv_directory, safe_filename)
        if not os.path.exists(path_to_file) or not validate_csv(path_to_file):
            return HttpResponse("Invalid CSV file", status=400)
        with open(path_to_file, 'rb') as csv_data:
            response = HttpResponse(csv_data, content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename=%s' % smart_str(safe_filename)
            return response
    return render(request, page + '/downloads.html', dic)

def validate_csv(csv_file_path: str)-> bool:
    is_valid_file = os.path.isfile(csv_file_path)
    is_csv_file = csv_file_path.endswith('.csv')
    return is_valid_file and is_csv_file

def analyze_csv(request):
    if request.method =='POST':
        if "_upload" in request.POST:
            file = request.FILES['csvFile']
            file_name = request.user.username + "_" + str(datetime.now()) + ".csv"
            dir_csvs = os.path.dirname(os.path.dirname(__file__)) + "/csvs/" + file_name
            with open(dir_csvs, 'wb+') as destination:
                for chunk in file.chunks():
                    destination.write(chunk)
            dictionary = {}
            return HttpResponseRedirect('/')
    return HttpResponseRedirect("/organization")

def coder_hash(request):
    if request.method == "POST":
        form = CoderHashForm(request.POST)
        if form.is_valid():
            form.save()
            return HttpResponseRedirect('/coder_hash')
    elif request.method == 'GET':
        return render(request, 'coder/coder-hash.html')

def sign_up_coder(request):
    flagCoder = 1
    flagHash = 0
    flagName = 0
    flagEmail = 0
    flagForm = 0
    if request.method == 'POST':
        form = CoderForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            password_confirm = form.cleaned_data['password_confirm']
            email = form.cleaned_data['email']
            email_confirm = form.cleaned_data['email_confirm']
            birthmonth = form.cleaned_data['birthmonth']
            birthyear = form.cleaned_data['birthyear']
            gender = form.cleaned_data['gender']
            country = form.cleaned_data['country']
            
            if User.objects.filter(username = username):
                flagName = 1
                return render(request, 'error/sign-up.html', {'flagName':flagName, 'flagEmail':flagEmail, 'flagHash':flagHash, 'flagForm':flagForm, 'flagCoder':flagCoder})
            elif User.objects.filter(email = email):
                flagEmail = 1
                return render(request, 'error/sign-up.html', {'flagName':flagName, 'flagEmail':flagEmail, 'flagHash':flagHash, 'flagForm':flag_form, 'flagCoder':flagCoder})
            elif (email != email_confirm):
                flagWrongEmail = 1
                return render(request, 'error/sign-up.html', {'flagName':flagName, 'flagEmail':flagEmail, 'flagHash':flagHash, 'flagForm':flagForm, 'flagCoder':flagCoder, 'flagWrongEmail': flagWrongEmail})
            elif (password != password_confirm):
                flagWrongPassword = 1
                return render(request, 'error/sign-up.html', {'flagName':flagName, 'flagEmail':flagEmail, 'flagHash':flagHash, 'flagForm':flagForm, 'flagCoder':flagCoder, 'flagWrongPassword': flagWrongPassword})
            else:
                coder = Coder.objects.create_user(username = username, email=email, password=password, birthmonth = birthmonth, birthyear = birthyear, gender = gender, country = country)
                coder = authenticate(username=username, password=password)
                login(request, coder)
                return HttpResponseRedirect('/coder/' + coder.username)
        else:
            flagForm = 1
            return render(request, 'error/sign-up.html', {'flagName':flagName, 'flagEmail':flagEmail, 'flagHash':flagHash, 'flagForm':flagForm})
    elif request.method == 'GET':
        if request.user.is_authenticated:
            return HttpResponseRedirect('/coder/' + request.user.username)
        else:
            return render(request, 'main/main.html')

def coder(request, name):
    if (request.method == 'GET') or (request.method == 'POST'):
        if request.user.is_authenticated:
            username = request.user.username
            if username == name:
                if Coder.objects.filter(username = username):
                    user = Coder.objects.get(username=username)
                    dic={'username':username, "img":str(user.img)}
                    return render(request, 'coder/main.html', dic)
                else:
                    logout(request)
                    return HttpResponseRedirect("/")
    else:
        return HttpResponseRedirect("/")

def login_coder(request):
    if request.method == 'POST':
        flagCoder = 0
        flag = False
        form = LoginOrganizationForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            coder = authenticate(username=username, password=password)
            if coder is not None:
                if coder.is_active:
                    login(request, coder)
                    return HttpResponseRedirect('/coder/' + coder.username)
            else:
                flag = True
                flagCoder = 1
                return render(request, 'sign-password/user-doesnt-exist.html', {'flag': flag, 'flagCoder': flagCoder})
    else:
        return HttpResponseRedirect("/")

def logout_coder(request):
    logout(request)
    return HttpResponseRedirect('/')

def change_pwd(request):
    if request.method == 'POST':
        recipient = request.POST['email']
        page = identify_user_type(request)
        try:
            if Organization.objects.filter(email=recipient):
                user = Organization.objects.get(email=recipient)
            elif Coder.objects.filter(email=recipient):
                user = Coder.objects.get(email=recipient)
        except:
            return render(request, 'sign-password/user-doesnt-exist.html')
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token=default_token_generator.make_token(user)
        c = {'email':recipient, 'uid':uid, 'token':token, 'id':user.username}
        body = render_to_string("sign-password/email-reset-pwd.html",c)
        subject = "Dr. Scratch: Did you forget your password?"
        sender ="no-reply@drscratch.org"
        to = [recipient]
        email = EmailMessage(subject,body,sender,to)
        email.send()
        return render(request, 'sign-password/email-sended.html')
    else:
        return render(request, 'sign-password/password.html')

def reset_password_confirm(request,uidb64=None,token=None,*arg,**kwargs):
    UserModel = get_user_model()
    try:
        uid = urlsafe_base64_decode(uidb64)
        if Organization.objects.filter(pk=uid):
            user = Organization._default_manager.get(pk=uid)
            page = 'organization'
        elif Coder.objects.filter(pk=uid):
            user = Coder._default_manager.get(pk=uid)
            page = 'coder'
    except (TypeError, ValueError, OverflowError, UserModel.DoesNotExist):
        user = None
    if request.method == "POST":
        flag_error = False
        if user is not None and default_token_generator.check_token(user, token):
            new_password = request.POST['password']
            new_confirm = request.POST['confirm']
            if new_password == "":
                return render(request, 'sign-password/new-password.html')
            elif new_password == new_confirm:
                user.set_password(new_password)
                user.save()
                logout(request)
                user = authenticate(username=user.username, password=new_password)
                login(request, user)
                return HttpResponseRedirect('/' + page + '/' + user.username)
            else:
                flag_error = True
                return render(request, 'sign-password/new-password.html', {'flag_error':flag_error})
    else:
         if user is not None and default_token_generator.check_token(user, token):
             return render(request, 'sign-password/new-password.html')
         else:
             return render(request, page + '/main.html')

def discuss(request):
    comments = dict()
    form = DiscussForm()
    if request.user.is_authenticated: user = request.user.username
    else: user = ""
    if request.method == "POST":
        form = DiscussForm(request.POST)
        if form.is_valid():
            nick = user
            date = timezone.now()
            comment = form.cleaned_data["comment"]
            new_comment = Discuss(nick = nick, date = date, comment=comment)
            new_comment.save()
        else: comments["form"] = form
    data = Discuss.objects.all().order_by("-date")
    lower = 0
    upper = 10
    list_comments = {}
    if len(data) > 10:
        for n in range(int((len(data)/10))+1):
            list_comments[str(n)]= data[lower:upper-1]
            lower = upper
            upper = upper + 10
    else: list_comments[0] = data
    comments["comments"] = list_comments
    return render(request, 'discuss.html', comments)

def error404(request):
    response = render(request, '404.html', {})
    response.status_code = 404
    return response

def date_range(start, end):
    r = (end+timedelta(days=1)-start).days
    return [start+timedelta(days=i) for i in range(r)]

def error500(request):
    response = render(request, '500.html', {})
    return response

def statistics(request):
    start = date(2015, 8, 1)
    end = datetime.today()
    date_list = date_range(start, end)
    my_dates = []
    for n in date_list: my_dates.append(n.strftime("%d/%m")) 
    obj = Stats.objects.order_by("-id")[0]
    data = {
        "date": my_dates,
        "dailyRate": obj.daily_score,
        "levels": { "basic": obj.basic, "development": obj.development, "master": obj.master },
        "totalProjects": obj.daily_projects,
        "skillRate": { "Parallelism": obj.parallelization, "abstraction": obj.abstraction, "logic": obj.logic, "synchronization": obj.synchronization, "flowControl": obj.flow_control, "userInteractivity": obj.userInteractivity, "dataRepresentation": obj.dataRepresentation },
        "codeSmellRate": { "deadCode": obj.deadCode, "duplicateScript": obj.duplicateScript, "spriteNaming": obj.spriteNaming, "initialization": obj.initialization }
    }
    return render(request, 'main/statistics.html', data)

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

def load_json_project(path_projectsb3):
    try:
        zip_file = ZipFile(path_projectsb3, "r")
        json_project = json.loads(zip_file.open("project.json").read())
        return json_project
    except BadZipfile:
        print('Bad zipfile')

# ==============================================================================
# BATCH MODE (NUEVA FUNCIONALIDAD AÑADIDA AL FINAL)
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
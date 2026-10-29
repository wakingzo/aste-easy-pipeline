import importlib.util, pathlib, math, sys, types
# stubs for import-time-only project modules
pm=types.ModuleType('project_env'); pm.load_project_env=lambda:{}; sys.modules['project_env']=pm
ap=types.ModuleType('aste_paths'); ap.ANNCSU_DIR=pathlib.Path('/tmp'); ap.REPORTS_DIR=pathlib.Path('/tmp'); ap.ensure_runtime_dirs=lambda:None; sys.modules['aste_paths']=ap
ROOT=pathlib.Path(__file__).resolve().parents[1]
def load(p,n):
 s=importlib.util.spec_from_file_location(n,p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
idx=load(ROOT/'src/geocoding/istat_locality_index_v1.py','idx')
aud=load(ROOT/'src/audit/locality_consistency_audit_v3_17.py','aud')
def test_utm_rome():
    lat,lon=idx.utm32_to_wgs84(790023.481206256,4644898.280632208)
    assert abs(lat-41.89)<0.08 and abs(lon-12.49)<0.08
def test_hint():
    assert aud.explicit_hint('Loc. Capo Comino, Podere 18, Siniscola NU').casefold()=='capo comino'
def test_red_istat_coarse():
    row={'latitudine':40.53,'longitudine':9.70,'geocode_precision':'PVP_IN_COMUNE','geocode_source':'pvp_comune_compatibile','indirizzo':'Loc. Capo Comino, Siniscola'}
    ev={'runtime_locality_source_hint':'Capo Comino','runtime_locality_match_mode':'EXACT','runtime_locality_lat':'','runtime_locality_lon':'','matched_locality_high_accuracy_accesses':0}
    im={'mode':'EXACT_UNIQUE','nome':'Capo Comino','lat':40.53,'lon':9.80}
    x=aud.classify(row,ev,im)
    assert x['severity']=='RED' and x['proposed_action']=='REVIEW_MOVE_TO_LOCALITA_SAFE'
def test_anncsu_near_ok():
    row={'latitudine':40.53,'longitudine':9.80,'geocode_precision':'PVP_IN_COMUNE','geocode_source':'pvp_comune_compatibile','indirizzo':'Loc. X, Comune'}
    ev={'runtime_locality_source_hint':'X','runtime_locality_match_mode':'EXACT','runtime_locality_lat':40.53,'runtime_locality_lon':9.80,'matched_locality_nearest_high_accuracy_access_m':100,'matched_locality_high_accuracy_accesses':10,'runtime_locality_p90_m':300,'runtime_anncsu_locality':'X'}
    x=aud.classify(row,ev,None);assert x['severity']=='OK'


def test_hint_dash_podere():
    assert aud.explicit_hint('Loc. Capo Comino - Podere 18, Siniscola NU').casefold()=='capo comino'

def test_bad_anncsu_geometry_not_red_without_safe_istat():
    row={'latitudine':38.7432,'longitudine':16.5545,'geocode_precision':'PVP_IN_COMUNE','geocode_source':'pvp_comune_compatibile','indirizzo':'Località Caminia, Stalettì'}
    ev={'runtime_locality_source_hint':'Caminia','runtime_locality_match_mode':'EXACT','runtime_locality_lat':41.9214707,'runtime_locality_lon':12.7134987,'matched_locality_nearest_high_accuracy_access_m':430794.17,'matched_locality_high_accuracy_accesses':243,'runtime_locality_p90_m':27812.76,'runtime_anncsu_locality':'CAMINIA'}
    x=aud.classify(row,ev,{'mode':'EXACT_AMBIGUOUS','score':1.0,'margin':0.0})
    assert x['severity']=='NEEDS_REF' and not x['repair_safe']

def test_safe_istat_red_is_repairable():
    row={'latitudine':40.5439613,'longitudine':9.7042263,'geocode_precision':'COMUNE','geocode_source':'istat_comune_indicativo','indirizzo':'loc capo comino, Siniscola'}
    ev={'runtime_locality_source_hint':'capo comino','runtime_locality_match_mode':'EXACT','runtime_locality_lat':'','runtime_locality_lon':'','matched_locality_high_accuracy_accesses':0}
    im={'mode':'EXACT_UNIQUE','nome':'Capo Comino','lat':40.5352359,'lon':9.7917778}
    x=aud.classify(row,ev,im)
    assert x['severity']=='RED' and x['repair_safe'] and x['proposed_action']=='REVIEW_MOVE_TO_LOCALITA_SAFE'

import importlib.util
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]/'src'/'geocoding'

def load(name):
    p=BASE/name
    s=importlib.util.spec_from_file_location(name.replace('.py',''),p)
    m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

def test_sqlite_runtime_matches_legacy_scanners(tmp_path):
    ann=load('anncsu_local_engine_v10.py')
    idx=load('anncsu_sqlite_index_v1.py')
    sq=load('anncsu_sqlite_runtime_v1.py')
    strad=tmp_path/'STRAD_ITA_test.csv'
    indir=tmp_path/'INDIR_ITA_test.csv'
    strad.write_text("CODICE_COMUNE;CODICE_ISTAT;PROGRESSIVO_NAZIONALE;CODICE_COMUNALE;ODONIMO;LOCALITA';TOTALE_ACCESSI;DIZIONE_LINGUA1;DIZIONE_LINGUA2\nA001;000001;100;;VIA ROMA;CENTRO;3;;\nA001;000001;101;;VIA GARIBALDI;BORGO;2;;\n",encoding='utf-8')
    indir.write_text("CODICE_COMUNE;CODICE_ISTAT;PROGRESSIVO_NAZIONALE;CODICE_COMUNALE;ODONIMO;DIZIONE_LINGUA1;DIZIONE_LINGUA2;PROGRESSIVO_ACCESSO;CODICE_COMUNALE_ACCESSO;CIVICO;ESPONENTE;SPECIFICITA;METRICO;PROGRESSIVO_SNC;COORD_X_COMUNE;COORD_Y_COMUNE;QUOTA;METODO\nA001;000001;100;;VIA ROMA;;;1;;10;;;;;12,5;41,9;;1\nA001;000001;100;;VIA ROMA;;;2;;12;A;;;;12,501;41,901;;3\nA001;000001;101;;VIA GARIBALDI;;;3;;1;;;;;12,51;41,91;;1\n",encoding='utf-8')
    db=tmp_path/'a.sqlite3'
    idx.build(strad,indir,db)
    rec=[{'pvp_id':'1','indirizzo':'Via Roma 12A','comune':'X','cap':'','sigla_provincia':'','expected_house_numbers':'12A'}]
    resolved={'1':'000001'}
    legacy=ann.scan_strad(strad,rec,resolved,topn=6)
    legacy_acc=ann.scan_indir(indir,{x[1] for x in legacy['1']})
    con=sq.open_index(db); sq.verify_index(con)
    new,_=sq.scan_streets(con,rec,resolved,ann,topn=6)
    new_acc=sq.load_accesses(con,{x[1] for x in new['1']},ann)
    assert dict(legacy)==dict(new)
    assert dict(legacy_acc)==dict(new_acc)
    assert sq.load_bboxes(con,{'000001'})['000001']['count']==3
    con.close()

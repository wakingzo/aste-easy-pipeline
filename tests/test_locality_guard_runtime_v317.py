import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src' / 'geocoding'))
import locality_guard_runtime_v1 as lg


def db(tmp_path, duplicate=False):
    p = tmp_path / 'loc.sqlite3'
    con = sqlite3.connect(p)
    con.execute('create table meta(key text primary key,value text)')
    con.execute('insert into meta values(?,?)',('version','istat_locality_index_v1'))
    con.execute('create table localities(pro_com text,loc21_id text,tipo_loc text,nome text,nome_norm text,lat real,lon real)')
    con.execute('insert into localities values(?,?,?,?,?,?,?)',('091085','x1','1','Capo Comino','capo comino',40.5352359031,9.7917777940))
    if duplicate:
        con.execute('insert into localities values(?,?,?,?,?,?,?)',('091085','x2','1','Capo Comino','capo comino',40.53,9.79))
    # satisfy index sanity threshold without changing queried municipality
    con.executemany('insert into localities values(?,?,?,?,?,?,?)', [('999999',f'z{i}','1',f'Z{i}',f'z{i}',41.0,12.0) for i in range(10000)])
    con.commit(); con.close(); return p


def ann_ev(**kw):
    out = {'anncsu_code_used':'091085','runtime_locality_match_mode':'EXPLICIT_UNMATCHED','runtime_locality_source_hint':'Capo Comino'}
    out.update(kw); return out


def coarse():
    return {'map_lat':40.5439613244,'map_lon':9.7042262619,'map_source':'istat_comune_indicativo','map_precision':'COMUNE','map_is_fallback':False}


def test_explicit_hint_podere():
    assert lg.explicit_hint('Loc. Capo Comino - Podere 18, Siniscola Nuoro') == 'Capo Comino'


def test_capo_comino_coarse_is_overridden(tmp_path):
    p=db(tmp_path); con=lg.open_index(p)
    row={'indirizzo':'Loc. Capo Comino - Podere 18, Siniscola Nuoro'}
    d=lg.evaluate(row, coarse(), {'final_precision':'COMUNE','final_source':'istat_comune_indicativo'}, ann_ev(), con)
    con.close()
    assert d['override'] is True
    assert d['action']=='OVERRIDE_TO_LOCALITA'
    assert d['distance_to_locality_m'] > 7000
    f,m=lg.apply_override(row, {'final_status':'DONE'}, coarse(), d)
    assert f['final_status']=='REVIEW' and f['final_precision']=='LOCALITA'
    assert m['map_precision']=='LOCALITA' and m['map_source']=='locality_guard_istat_localita_2021'


def test_precise_coordinate_is_never_displaced(tmp_path):
    p=db(tmp_path); con=lg.open_index(p)
    m=coarse(); m.update(map_precision='CIVICO',map_source='google')
    d=lg.evaluate({'indirizzo':'Loc. Capo Comino, Siniscola'}, m, {'final_precision':'CIVICO','final_source':'google'}, ann_ev(), con)
    con.close()
    assert d['override'] is False
    assert d['coarse_candidate'] is False


def test_exact_ambiguous_fails_closed(tmp_path):
    p=db(tmp_path, duplicate=True); con=lg.open_index(p)
    d=lg.evaluate({'indirizzo':'Loc. Capo Comino, Siniscola'}, coarse(), {'final_precision':'COMUNE'}, ann_ev(), con)
    con.close()
    assert d['override'] is False and d['action']=='NO_UNIQUE_ISTAT_LOCALITY'


def test_reference_divergence_fails_closed(tmp_path):
    p=db(tmp_path); con=lg.open_index(p)
    ev=ann_ev(runtime_locality_match_mode='EXACT',runtime_locality_lat=41.0,runtime_locality_lon=10.5,runtime_locality_p90_m=100)
    d=lg.evaluate({'indirizzo':'Loc. Capo Comino, Siniscola'}, coarse(), {'final_precision':'COMUNE'}, ev, con)
    con.close()
    assert d['override'] is False
    assert d['reference_warning']=='ANNCSU_ISTAT_REFERENCE_DIVERGENCE'


def test_no_explicit_locality_noop(tmp_path):
    p=db(tmp_path); con=lg.open_index(p)
    d=lg.evaluate({'indirizzo':'Via Roma 1, Siniscola'}, coarse(), {'final_precision':'COMUNE'}, {}, con)
    con.close()
    assert d == {'action':'NO_EXPLICIT_LOCALITY','override':False}

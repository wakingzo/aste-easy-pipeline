"""v3.8 P1-P5 policy dry-run: no network, no DB, conservative map fallback."""
from __future__ import annotations
import csv
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'src'
paths = types.ModuleType('aste_paths')
paths.REPORTS_DIR = Path(tempfile.gettempdir())
paths.ensure_runtime_dirs = lambda: None
sys.modules['aste_paths'] = paths
geo = types.ModuleType('geocode_pending_v2')
geo.SELECT = ('pvp_id','comune','sigla_provincia','indirizzo','pvp_latitudine','pvp_longitudine','geocode_status')
geo.valid_pair = lambda la,lo: la is not None and lo is not None and not (float(la)==0 and float(lo)==0)
geo.pvp_row = lambda row: dict(row, latitudine=row.get('pvp_latitudine'), longitudine=row.get('pvp_longitudine'))
geo.prepare_context = lambda **kw: (None,None,None)
geo.plan_rows = lambda *a: []
sys.modules['geocode_pending_v2'] = geo
sys.path.insert(0, str(SRC / 'geocoding'))
import google_policy_v3_8 as m

class Ctx:
    def resolve_admin(self,row):
        if row.get('comune') == 'Ambiguo':
            return {'status':'REVIEW'}
        return {'status':'RESOLVED','canonical_code':'090070'}
    def representative_point(self,row,admin_meta=None):
        return {'ok':True,'lat':40.90,'lon':9.10}
    def test(self,row,lat=None,lon=None,admin_meta=None):
        return {'position':'INSIDE'}

class PolicyTests(unittest.TestCase):
    def row(self, **kw):
        r={'pvp_id':'1','comune':'Tempio Pausania','sigla_provincia':'SS','indirizzo':'Via Roma 5',
           'pvp_latitudine':None,'pvp_longitudine':None,'geocode_status':'PENDING'}
        r.update(kw); return r
    def base(self, **kw):
        b={'geocode_status':'DONE','geocode_source':'istat_comune_indicativo','geocode_precision':'COMUNE',
           'latitudine':40.91,'longitudine':9.11,'geocode_decision_reason':'baseline'}
        b.update(kw); return b
    def ev(self, label='P2_SENZA_PVP_CON_CIVICO', **kw):
        e={'priority_label':label,'assessment':'GOOGLE_CIVICO_ESATTO_DA_VALIDARE',
           'google_location_type':'ROOFTOP','google_civic_relation':'exact','google_istat_position':'INSIDE',
           'google_score_diagnostico':'103','google_lat':'40.92','google_lon':'9.12',
           'google_distance_from_pvp_m':''}
        e.update(kw); return e

    def test_p2_rooftop_exact_becomes_civico(self):
        z=m.apply_policy(self.row(),self.base(),self.ev())
        self.assertEqual(z['policy_action'],'P2_PROMOTE_GOOGLE_CIVICO')
        self.assertEqual(z['final_precision'],'CIVICO')
        self.assertEqual(z['final_source'],'google_civico_rooftop_v38')

    def test_p2_interpolated_is_not_civico(self):
        z=m.apply_policy(self.row(),self.base(),self.ev(google_location_type='RANGE_INTERPOLATED'))
        self.assertEqual(z['policy_action'],'P2_USE_GOOGLE_INTERPOLATED_AS_STRADA')
        self.assertEqual(z['final_precision'],'STRADA')

    def test_p2_variant_stays_baseline(self):
        z=m.apply_policy(self.row(),self.base(),self.ev(google_civic_relation='variant'))
        self.assertEqual(z['policy_action'],'KEEP_OFFLINE_BASELINE')
        self.assertEqual(z['final_precision'],'COMUNE')

    def test_p3_coincident_keeps_pvp(self):
        base=self.base(geocode_source='pvp_comune_compatibile',geocode_precision='PVP_IN_COMUNE',latitudine=40.92,longitudine=9.12)
        z=m.apply_policy(self.row(pvp_latitudine=40.92,pvp_longitudine=9.12),base,
                         self.ev('P3_PVP_NON_CONFERMATO_CON_CIVICO',google_distance_from_pvp_m='0'))
        self.assertEqual(z['policy_action'],'P3_KEEP_PVP_GOOGLE_NON_INDIPENDENTE')
        self.assertEqual(z['final_source'],'pvp_comune_compatibile')

    def test_p3_distinct_within_150_becomes_civico(self):
        base=self.base(geocode_source='pvp_comune_compatibile',geocode_precision='PVP_IN_COMUNE',latitudine=40.90,longitudine=9.10)
        z=m.apply_policy(self.row(pvp_latitudine=40.90,pvp_longitudine=9.10),base,
                         self.ev('P3_PVP_NON_CONFERMATO_CON_CIVICO',google_distance_from_pvp_m='80'))
        self.assertEqual(z['policy_action'],'P3_PROMOTE_GOOGLE_CIVICO')
        self.assertEqual(z['final_precision'],'CIVICO')

    def test_p3_over_150_is_review_without_detailed_pin(self):
        base=self.base(geocode_source='pvp_comune_compatibile',geocode_precision='PVP_IN_COMUNE',latitudine=40.90,longitudine=9.10)
        z=m.apply_policy(self.row(pvp_latitudine=40.90,pvp_longitudine=9.10),base,
                         self.ev('P3_PVP_NON_CONFERMATO_CON_CIVICO',google_distance_from_pvp_m='180'))
        self.assertEqual(z['policy_action'],'P3_REVIEW_GOOGLE_PVP_DIVERGENCE')
        self.assertEqual(z['final_status'],'REVIEW')
        self.assertIsNone(z['final_lat'])

    def test_p1_always_review(self):
        z=m.apply_policy(self.row(),self.base(),self.ev('P1_DISCORDANZA_PVP_ANNCSU'))
        self.assertEqual(z['policy_action'],'P1_REVIEW_NO_AUTO_REPLACEMENT')
        self.assertEqual(z['final_status'],'REVIEW')
        self.assertIsNone(z['final_lat'])


    def test_p4_geometric_center_improves_only_to_street(self):
        ev=self.ev('P4_SENZA_PVP_TOPONIMO',assessment='INDIZIO_NON_PRECISO_NON_SOSTITUIRE',
                   google_location_type='GEOMETRIC_CENTER',google_civic_relation='no_expected',
                   address_kind='STRADA',google_reason='comune_ok|provincia_ok|indirizzo_match=1.00|geometric_center_not_repairable')
        z=m.apply_policy(self.row(indirizzo='Via Ravel, Tempio Pausania'),self.base(),ev)
        self.assertEqual(z['policy_action'],'P4_USE_GOOGLE_TOPONIMO_STRADA')
        self.assertEqual(z['final_precision'],'STRADA')

    def test_p4_multicivic_rooftop_uses_one_representative_civic(self):
        ev=self.ev('P4_SENZA_PVP_TOPONIMO',assessment='POSSIBILE_MIGLIORAMENTO_DA_VERIFICARE',
                   google_civic_relation='no_expected',google_location_type='ROOFTOP',
                   google_formatted_address='Via Podgora, 9/29, 07029 Tempio Pausania SS, Italia',
                   google_reason='comune_ok|provincia_ok|indirizzo_match=1.00|civico_ok')
        z=m.apply_policy(self.row(indirizzo='Via Podgora, 9 e 29, 07029 Tempio Pausania SS'),self.base(),ev)
        self.assertEqual(z['policy_action'],'P4_PROMOTE_GOOGLE_CIVICO_RAPPRESENTATIVO')
        self.assertEqual(z['final_precision'],'CIVICO')

    def test_p5_geometric_center_near_keeps_pvp_and_confirms_street(self):
        base=self.base(geocode_source='pvp_comune_compatibile',geocode_precision='PVP_IN_COMUNE',latitudine=40.90,longitudine=9.10)
        ev=self.ev('P5_PVP_NON_CONFERMATO_TOPONIMO',assessment='INDIZIO_NON_PRECISO_NON_SOSTITUIRE',
                   google_location_type='GEOMETRIC_CENTER',google_civic_relation='no_expected',address_kind='STRADA',
                   google_distance_from_pvp_m='80',google_reason='comune_ok|provincia_ok|indirizzo_match=1.00|geometric_center_not_repairable')
        z=m.apply_policy(self.row(pvp_latitudine=40.90,pvp_longitudine=9.10,indirizzo='Via Ravel, Tempio Pausania'),base,ev)
        self.assertEqual(z['policy_action'],'P5_KEEP_PVP_CONFIRM_TOPONIMO_STRADA')
        self.assertEqual(z['final_precision'],'PVP_STRADA_COMPATIBILE')
        self.assertEqual((z['final_lat'],z['final_lon']),(40.90,9.10))

    def test_p5_geometric_center_coincident_not_independent(self):
        base=self.base(geocode_source='pvp_comune_compatibile',geocode_precision='PVP_IN_COMUNE',latitudine=40.90,longitudine=9.10)
        ev=self.ev('P5_PVP_NON_CONFERMATO_TOPONIMO',assessment='INDIZIO_NON_PRECISO_NON_SOSTITUIRE',
                   google_location_type='GEOMETRIC_CENTER',google_civic_relation='no_expected',address_kind='STRADA',
                   google_distance_from_pvp_m='0',google_reason='comune_ok|provincia_ok|indirizzo_match=1.00|geometric_center_not_repairable')
        z=m.apply_policy(self.row(pvp_latitudine=40.90,pvp_longitudine=9.10,indirizzo='Via Ravel, Tempio Pausania'),base,ev)
        self.assertEqual(z['policy_action'],'P5_KEEP_PVP_GOOGLE_NON_INDIPENDENTE')
        self.assertEqual(z['final_precision'],'PVP_IN_COMUNE')


    def test_p4_suffix_variant_is_not_promoted_to_civic(self):
        ev=self.ev('P4_SENZA_PVP_TOPONIMO',assessment='INDIZIO_NON_PRECISO_NON_SOSTITUIRE',
                   google_civic_relation='no_expected',google_location_type='ROOFTOP',address_kind='STRADA',
                   google_formatted_address='Via Silvio Pellico, 24, 07029 Tempio Pausania SS, Italia',
                   google_reason='comune_ok|provincia_ok|indirizzo_match=1.00|civico_variant')
        z=m.apply_policy(self.row(indirizzo='Via Silvio Pellico, 24A-26A, 07029 Tempio Pausania SS'),self.base(),ev)
        self.assertEqual(z['policy_action'],'KEEP_OFFLINE_BASELINE')

    def test_p5_without_usable_pvp_can_use_google_toponym(self):
        base=self.base(geocode_source='istat_comune_indicativo',geocode_precision='COMUNE',latitudine=40.90,longitudine=9.10)
        ev=self.ev('P5_PVP_NON_CONFERMATO_TOPONIMO',assessment='INDIZIO_NON_PRECISO_NON_SOSTITUIRE',
                   google_location_type='GEOMETRIC_CENTER',google_civic_relation='no_expected',address_kind='STRADA',
                   google_distance_from_pvp_m='3000',google_reason='comune_ok|provincia_ok|indirizzo_match=1.00|geometric_center_not_repairable')
        z=m.apply_policy(self.row(pvp_latitudine=41.0,pvp_longitudine=9.2,indirizzo='Via Galliera, Tempio Pausania'),base,ev)
        self.assertEqual(z['policy_action'],'P5_USE_GOOGLE_TOPONIMO_SENZA_PVP_STRADA')
        self.assertEqual(z['final_precision'],'STRADA')

    def test_p5_representative_rooftop_near_promotes_civic(self):
        base=self.base(geocode_source='pvp_comune_compatibile',geocode_precision='PVP_IN_COMUNE',latitudine=40.90,longitudine=9.10)
        ev=self.ev('P5_PVP_NON_CONFERMATO_TOPONIMO',assessment='GOOGLE_COMPATIBILE_CON_PVP_DA_VERIFICARE',
                   google_location_type='ROOFTOP',google_civic_relation='no_expected',address_kind='STRADA',
                   google_distance_from_pvp_m='80',google_formatted_address='Via Roma, 66, 07029 Tempio Pausania SS, Italia',
                   google_reason='comune_ok|provincia_ok|indirizzo_match=1.00|civico_ok')
        z=m.apply_policy(self.row(pvp_latitudine=40.90,pvp_longitudine=9.10,indirizzo='Via Roma, 66 - 68 - 70, 07029 Tempio Pausania SS'),base,ev)
        self.assertEqual(z['policy_action'],'P5_PROMOTE_GOOGLE_CIVICO_RAPPRESENTATIVO')
        self.assertEqual(z['final_precision'],'CIVICO')

    def test_p5_representative_rooftop_far_goes_review(self):
        base=self.base(geocode_source='pvp_comune_compatibile',geocode_precision='PVP_IN_COMUNE',latitudine=40.90,longitudine=9.10)
        ev=self.ev('P5_PVP_NON_CONFERMATO_TOPONIMO',assessment='GOOGLE_DIVERGE_DA_PVP_DA_VERIFICARE',
                   google_location_type='ROOFTOP',google_civic_relation='no_expected',address_kind='STRADA',
                   google_distance_from_pvp_m='500',google_formatted_address='Via Roma, 66, 07029 Tempio Pausania SS, Italia',
                   google_reason='comune_ok|provincia_ok|indirizzo_match=1.00|civico_ok')
        z=m.apply_policy(self.row(pvp_latitudine=40.90,pvp_longitudine=9.10,indirizzo='Via Roma, 66 - 68 - 70, 07029 Tempio Pausania SS'),base,ev)
        self.assertEqual(z['policy_action'],'P5_REVIEW_GOOGLE_PVP_CIVICO_DIVERGENCE')
        self.assertEqual(z['final_status'],'REVIEW')
        self.assertIsNone(z['final_lat'])

    def test_map_uses_final_coordinate_when_available(self):
        z=m.choose_map_point(self.row(),m.apply_policy(self.row(),self.base(),None),Ctx(),{})
        self.assertFalse(z['map_is_fallback'])
        self.assertEqual(z['map_precision'],'COMUNE')

    def test_map_falls_back_to_verified_municipality_for_review(self):
        final={'final_lat':None,'final_lon':None,'final_source':'review','final_precision':'NESSUNA'}
        z=m.choose_map_point(self.row(),final,Ctx(),{})
        self.assertTrue(z['map_is_fallback'])
        self.assertEqual(z['map_precision'],'COMUNE')
        self.assertEqual((z['map_lat'],z['map_lon']),(40.9,9.1))

    def test_map_does_not_fake_location_when_admin_unresolved(self):
        final={'final_lat':None,'final_lon':None,'final_source':'review','final_precision':'NESSUNA'}
        z=m.choose_map_point(self.row(comune='Ambiguo'),final,Ctx(),{})
        self.assertEqual(z['map_precision'],'NESSUNA')
        self.assertIsNone(z['map_lat'])

    def test_latest_report_wins_and_old_without_priority_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            td=Path(td)
            (td/'google_comparison_v3_20260101.csv').write_text(
                'pvp_id;assessment\n1;OLD\n',encoding='utf-8-sig')
            (td/'google_comparison_v3_20260102.csv').write_text(
                'pvp_id;priority_label;assessment\n1;P2_SENZA_PVP_CON_CIVICO;NEW\n',encoding='utf-8-sig')
            ev,files,n=m.load_google_evidence(td)
        self.assertEqual(ev['1']['assessment'],'NEW')
        self.assertEqual(n,1)
        self.assertEqual(len(files),2)

    def test_full_preview_zero_google_calls_and_map_coverage(self):
        row=self.row()
        base=self.base(latitudine=None,longitudine=None,geocode_status='REVIEW',geocode_precision='NESSUNA')
        with tempfile.TemporaryDirectory() as td:
            td=Path(td)
            (td/'google_comparison_v3_x.csv').write_text(
                'pvp_id;priority_label;assessment\n',encoding='utf-8-sig')
            with patch.object(m,'REPORTS_DIR',td), patch.object(m,'ensure_runtime_dirs'), \
                 patch.object(m.offline,'prepare_context',return_value=(Ctx(),object(),object())), \
                 patch.object(m.offline,'plan_rows',return_value=[(row,base)]):
                out=m.preview_new_rows([row],max_rows=1)
        self.assertEqual(out['google_calls'],0)
        self.assertEqual(out['policy_preview']['map_coverage']['mapped'],1)
        self.assertEqual(out['policy_preview']['map_coverage']['fallback_mapped'],1)

if __name__=='__main__':
    unittest.main(verbosity=2)

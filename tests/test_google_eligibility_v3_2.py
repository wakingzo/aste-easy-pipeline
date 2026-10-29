"""No HTTP/DB/Google imports; deterministic v3.4 eligibility/parser tests."""
import os
import sys
import tempfile
import unittest
import types
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'src'/'geocoding'))
import google_eligibility_v3_2 as gate


def row(address='Via Roma 5, 07029 Tempio Pausania SS', **extra):
    d={'pvp_id':'1','indirizzo':address,'comune':'Tempio Pausania','provincia':'Sassari',
       'sigla_provincia':'SS','regione':'Sardegna','pvp_geo_source':'bene:1/1',
       'pvp_latitudine':40.9,'pvp_longitudine':9.1}
    d.update(extra)
    return d


def decision(source='pvp_comune_compatibile',precision='PVP_IN_COMUNE',status='DONE'):
    return {'geocode_source':source,'geocode_precision':precision,'geocode_status':status}


class Eligibility(unittest.TestCase):
    def test_via_named_without_house_number_is_useful(self):
        self.assertTrue(gate.eligibility(row('Via San Giovanni Bosco, Tempio Pausania'),decision())['google_eligible'])

    def test_new_valid_prefixes_are_searchable_and_parse_civic(self):
        cases = (
            ('Piazzetta Cesare Battisti civico 2, 73100 Lecce Lecce','Lecce','STRADA','Cesare Battisti','2'),
            ('Str. del Drosso, 128/23, 10135 Torino Torino','Torino','STRADA','del Drosso','128/23'),
            ('Lungomare C. Colombo n. 10, 76125 Trani Barletta-Andria-Trani','Trani','STRADA','C. Colombo','10'),
            ('Sestiere Cannaregio 4050, 30121 Venezia Venezia','Venezia','LOCALITA','Cannaregio','4050'),
            ('Regione Borgnalle, 12, 11100 Aosta AO, Aosta Aosta','Aosta','LOCALITA','Borgnalle','12'),
        )
        for address, comune, kind, name, civic in cases:
            with self.subTest(address=address):
                r=row(address,comune=comune)
                p=gate.address_parts(r)
                self.assertEqual((p['kind'],p['name'],p['civic']),(kind,name,civic))
                self.assertTrue(gate.eligibility(r,decision())['google_eligible'])

    def test_trailing_admin_after_explicit_civic_is_not_part_of_street(self):
        p=gate.address_parts(row('Via Pier Ludovico Occhini n.6 Arezzo, 52100 Arezzo Arezzo',
                                 comune='Arezzo',provincia='Arezzo',sigla_provincia='AR',regione='Toscana'))
        self.assertEqual(p['name'],'Pier Ludovico Occhini')
        self.assertEqual(p['civic'],'6')

    def test_slash_letter_civic_is_parsed(self):
        p=gate.address_parts(row('CONTRADA FOSSO A MARE 32/B, 62018 Potenza Picena Macerata',
                                 comune='Potenza Picena',provincia='Macerata',sigla_provincia='MC'))
        self.assertEqual(p['kind'],'LOCALITA')
        self.assertEqual(p['name'],'FOSSO A MARE')
        self.assertEqual(p['civic'],'32/B')


    def test_multi_civic_uses_one_representative_civic_without_rewriting_source(self):
        cases = (
            ('Via Podgora, 9 e 29, 40062 Molinella Bologna','9'),
            ('Via Giovanni Matteo Adami, 31-33-35, 91026 Mazara del Vallo Trapani','31'),
            ('Via Roma, 66 – 68 – 70, 87032 Amantea Cosenza','66'),
            ('Via Silvio Pellico, 24A-26A, 36015 Schio Vicenza','24A'),
        )
        for address, expected in cases:
            with self.subTest(address=address):
                r=row(address,comune='Molinella')
                before=r['indirizzo']
                out=gate.address_parts(r)
                self.assertEqual(out['civic'],expected)
                self.assertEqual(r['indirizzo'],before)

    def test_next_piece_civic_with_cap_tail_is_recognized(self):
        p=gate.address_parts(row("Via Enrico Fermi, 6B 09045 Quartu Sant'Elena CA",
                                 comune="Quartu Sant'Elena",provincia='Cagliari',sigla_provincia='CA'))
        self.assertEqual(p['civic'],'6B')

    def test_pvp_has_no_coords_and_named_locality_without_civic_bypasses_google(self):
        x=gate.eligibility(row('Località Sa Petra Ruja, 08029 Siniscola',comune='Siniscola',
                               pvp_latitudine=None,pvp_longitudine=None),decision('istat_comune_indicativo','COMUNE'))
        self.assertFalse(x['google_eligible'])
        self.assertEqual(x['google_reason'],'LOCALITA_TRONCATA_USA_ANNCSU_O_COMUNE')
        self.assertEqual(x['address_kind'],'LOCALITA')

    def test_named_locality_with_civic_can_still_use_google_if_anncsu_unresolved(self):
        x=gate.eligibility(row('Contrada Fosso a Mare 32/B, 62018 Potenza Picena Macerata',
                               comune='Potenza Picena',provincia='Macerata',sigla_provincia='MC',
                               pvp_latitudine=None,pvp_longitudine=None),
                           decision('istat_comune_indicativo','COMUNE'))
        self.assertTrue(x['google_eligible'])
        self.assertEqual(x['address_kind'],'LOCALITA')
        self.assertEqual(x['address_civic'],'32/B')

    def test_anncsu_resolved_never_google(self):
        for source,prec in [('anncsu_civico','CIVICO'),('anncsu_strada','STRADA'),
                            ('pvp_anncsu_civico_compatibile','PVP_CIVICO_COMPATIBILE'),
                            ('pvp_anncsu_strada_compatibile','PVP_STRADA_COMPATIBILE'),
                            ('pvp_anncsu_localita_compatibile','PVP_LOCALITA_COMPATIBILE')]:
            with self.subTest(source=source):
                self.assertFalse(gate.eligibility(row(),decision(source,prec))['google_eligible'])

    def test_discrepancy_named_address_can_google(self):
        for source in ('review_pvp_civico_discordante','review_pvp_strada_discordante','review_pvp_localita_discordante'):
            with self.subTest(source=source):
                self.assertEqual(gate.eligibility(row(),decision(source,'NESSUNA','REVIEW'))['google_group'],'REVIEW_DISCORDANZA')

    def test_municipality_only_cadastral_agro_and_placeholders_no_google(self):
        for addr in ('Tempio Pausania','Comune di Tempio Pausania','07029 Tempio Pausania SS','',
                     'zona periferica, Tempio Pausania','agro di Putignano, 70017 Putignano',
                     'FOGLIO 12 MAPPALE 1134-1133, 09017 Sant Antioco',
                     'Via nd, 66010 Canosa Sannita Chieti','Via n.d., Canosa Sannita',
                     'Via snc, Tempio Pausania','Località sconosciuta, Tempio Pausania'):
            with self.subTest(addr=addr):
                self.assertFalse(gate.eligibility(row(addr),decision('istat_comune_indicativo','COMUNE'))['google_eligible'])

    def test_multibene_no_google_even_named_address(self):
        self.assertEqual(gate.eligibility(row(pvp_geo_source='bene_multi:1/2'),decision())['google_reason'],'PIU_BENI_NON_DISAMBIGUATI')

    def test_alt_municipality_no_google_without_lot_review(self):
        self.assertFalse(gate.eligibility(row(),decision('pvp_comune_alternativo','PVP_INDICATIVO_ALTRO_COMUNE','REVIEW'))['google_eligible'])

    def test_compound_address_one_road_one_locality_allowed(self):
        self.assertTrue(gate.eligibility(row('Via Roma 5, Località Sa Petra Ruja, 07029 Tempio Pausania'),decision())['google_eligible'])

    def test_compound_address_two_different_roads_blocked(self):
        self.assertEqual(gate.eligibility(row('Via Roma 5, Via Milano 8'),decision())['google_reason'],'PIU_TOPONIMI_NON_DISAMBIGUATI')

    def test_no_verified_admin_no_google(self):
        self.assertFalse(gate.eligibility(row(comune=''),decision())['google_eligible'])

    def test_no_change_to_coordinate_source(self):
        r=row(); d=decision();before_r=r.copy();before_d=d.copy();gate.eligibility(r,d)
        self.assertEqual(r,before_r); self.assertEqual(d,before_d)

    def test_putignano_named_ss_is_unverified_not_repaired(self):
        for address, expected in (
            ('SS Alberobello n. 45, 70017 Putignano Bari','STRADA_EXTRAURBANA_NON_VERIFICATA'),
            ('SS 47 Alberobello n.45, 70017 Putignano Bari','STRADA_EXTRAURBANA_NUMERO_E_NOME_NON_VERIFICATI'),
        ):
            with self.subTest(address=address):
                original=row(address,comune='Putignano',pvp_latitudine=None,pvp_longitudine=None)
                snapshot=dict(original); out=gate.eligibility(original,decision('istat_comune_indicativo','COMUNE'))
                self.assertFalse(out['google_eligible']); self.assertEqual(out['google_reason'],expected)
                self.assertEqual(original,snapshot); self.assertNotIn('172',str(out))

    def test_numeric_extraurban_route_is_specific_but_not_invented(self):
        for address in ('SS 172, Putignano','SP 28, Usini','Strada Provinciale 55, Montalcino'):
            with self.subTest(address=address):
                out=gate.eligibility(row(address),decision())
                self.assertTrue(out['google_eligible'])
                self.assertEqual(out['address_kind'],'STRADA')
                self.assertRegex(out['address_name'],r'(?i)(ss|sp|strada provinciale)\s+\d+')

    def test_extraurban_mixed_is_not_stitched(self):
        for address in ('SP 28 Prolungamento Via Diaz, Usini','Contrada Turrisi, Strada Statale 113, Partinico'):
            with self.subTest(address=address):
                outcome=gate.eligibility(row(address),decision())
                self.assertFalse(outcome['google_eligible'])
                self.assertIn(outcome['google_reason'],{'INDICAZIONI_STRADALI_MISTE_DA_VERIFICARE','STRADA_EXTRAURBANA_E_LOCALITA_DA_DISAMBIGUARE'})


    def test_prefix_word_inside_single_street_name_is_not_split(self):
        p=gate.address_parts(row('Via Rione Cacia, Ciminà Reggio di Calabria',comune='Ciminà'))
        self.assertEqual(p['kind'],'STRADA'); self.assertEqual(p['name'],'Rione Cacia')
        p=gate.address_parts(row('Traversa Di Via San Rocco n. 10, 86020 Campochiaro Campobasso',comune='Campochiaro'))
        self.assertEqual(p['kind'],'STRADA'); self.assertEqual(p['name'],'Di Via San Rocco'); self.assertEqual(p['civic'],'10')

    def test_street_abbreviation_and_civic_not_confused_with_cap(self):
        for address,name,civic in (
            ('V. Giacomo Leopardi, 26, 63827 Pedaso FM','Giacomo Leopardi','26'),
            ('V. Compagnetto da Prato, 6/1, 59100 Prato PO','Compagnetto da Prato','6/1'),
            ('Via 4 Novembre 25, Nuoro','4 Novembre','25'),
            ('Via Roma, 5, 00100 Roma','Roma','5'),
        ):
            with self.subTest(address=address):
                result=gate.address_parts(row(address,comune='Pedaso'))
                self.assertEqual(result['name'],name); self.assertEqual(result['civic'],civic)
        self.assertEqual(gate.address_parts(row('Via 4 Novembre, Nuoro'))['civic'],'')
        self.assertEqual(gate.address_parts(row('Via Roma, 00100 Roma'))['civic'],'')

    def test_offline_plan_no_google_key_no_network_with_mocks(self):
        # Isolated test: the overlay ZIP intentionally does not bundle the base pipeline modules.
        fake_paths=types.ModuleType('aste_paths'); fake_paths.REPORTS_DIR=Path(tempfile.gettempdir()); fake_paths.ensure_runtime_dirs=lambda:None
        fake_offline=types.ModuleType('geocode_pending_v2'); fake_offline.SELECT=('pvp_id','comune','indirizzo','pvp_geo_source','pvp_latitudine','pvp_longitudine','geocode_status')
        fake_offline.prepare_context=lambda **kwargs:(object(),object(),object()); fake_offline.plan_rows=lambda *args:[]; fake_offline.valid_pair=lambda a,b:a is not None and b is not None
        with patch.dict(sys.modules,{'aste_paths':fake_paths,'geocode_pending_v2':fake_offline}):
            sys.modules.pop('google_policy_plan_v3_2',None)
            import google_policy_plan_v3_2 as planner
        new=row('Località Sa Petra Ruja',pvp_latitudine=None,pvp_longitudine=None,geocode_status='PENDING')
        with tempfile.TemporaryDirectory() as td, patch.object(planner,'REPORTS_DIR',Path(td)), \
             patch.object(planner,'ensure_runtime_dirs'), \
             patch.object(planner.offline,'prepare_context',return_value=(object(),object(),object())), \
             patch.object(planner.offline,'plan_rows',return_value=[(new,decision('istat_comune_indicativo','COMUNE'))]), \
             patch.dict(os.environ,{'GOOGLE_GEOCODING_API_KEY':''}):
            out=planner.preview_new_rows([new],max_rows=1)
            self.assertTrue(Path(out['google_policy_plan']['report_csv']).exists())
        self.assertEqual(out['google_calls'],0); self.assertEqual(out['google_policy_plan']['eligible_count'],0)
        self.assertEqual(out['google_policy_plan']['policy_version'],'GOOGLE_AFTER_ANNCSU_V3_4')

if __name__=='__main__': unittest.main(verbosity=2)

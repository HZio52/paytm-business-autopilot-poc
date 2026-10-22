"""Logic tests. Run from the project folder:  python -m unittest backend.tests.test_core -v"""
import os, json, tempfile, unittest
os.environ.setdefault("AUTOPILOT_DB_PATH", os.path.join(tempfile.gettempdir(), "autopilot-test.db"))
from backend import db, core, seed

def state():
    with db.reading() as c: return core.frontend_state(c)

def w(fn, *a, **k):
    with db.writing() as c: return fn(c, *a, **k)

class T(unittest.TestCase):
    def setUp(self): seed.reset()

    def test_seed_story(self):
        L = state()['live']
        self.assertEqual(L['briefing']['eligible_count'], 14)
        self.assertEqual(L['latest_result']['net_incremental'], 940)
        self.assertEqual(L['briefing']['skipped_seasonal_count'], 6)
        self.assertNotEqual(L['briefing']['predicted_net'], L['latest_result']['net_incremental'])

    def test_deterministic_reset(self):
        a = state(); seed.reset(); b = state()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_approve_cancel_approve(self):
        exp = w(core.start_experiment, 'voice', 'hi')
        L = state()['live']
        self.assertEqual(L['briefing']['status'], 'running')
        self.assertEqual(len(L['running']['offer_messages']), 7)
        self.assertIn('छूट', L['running']['offer_messages'][0]['text'])
        with self.assertRaises(core.ApiError) as e: w(core.start_experiment)
        self.assertEqual(e.exception.status, 409)
        w(core.cancel_experiment, exp)
        self.assertEqual(state()['live']['briefing']['status'], 'awaiting_approval')
        self.assertEqual(state()['live']['briefing']['eligible_count'], 14)
        w(core.start_experiment)

    def test_fast_forward_completes_and_learns(self):
        w(core.start_experiment)
        r = w(core.advance, 21)
        self.assertEqual(len(r['completed_experiments']), 1)
        res = r['completed_experiments'][0]
        self.assertEqual(res['offer_count'], 7)
        L = state()['live']
        self.assertEqual(L['latest_result']['experiment_id'], res['experiment_id'])
        self.assertLess(L['learned']['expected_return_rate'], 0.35)  # 2 of 7 came back, so it lowers its estimate
        self.assertEqual(L['days_advanced'], 21)
        self.assertEqual(state()['frontend']['daysForward'], 3)
        with self.assertRaises(core.ApiError): w(core.cancel_experiment, res['experiment_id'])

    def test_fast_forward_without_approval_grows_list(self):
        w(core.advance, 21)
        b = state()['live']['briefing']
        self.assertGreater(b['eligible_count'], 14)

    def test_new_week(self):
        before = state()['live']['metrics']['revenue_week']
        r = w(core.advance, 7, 'Started a new week')
        self.assertGreater(r['new_payments'], 50)

    def test_add_customer_rules(self):
        for bad in ['12345', '5123456789', 'abc']:
            with self.assertRaises(core.ApiError) as e: w(core.add_customer, 'Test Person', bad)
            self.assertEqual(e.exception.status, 422)
        with self.assertRaises(core.ApiError): w(core.add_customer, 'A', '9876543210')
        cid = w(core.add_customer, 'Test Person', '+91 98765 43210')
        with self.assertRaises(core.ApiError) as e: w(core.add_customer, 'Other', '09876543210')
        self.assertEqual(e.exception.status, 409); self.assertEqual(e.exception.extra['customer_id'], cid)
        with db.reading() as c: d = core.customer_detail(c, cid)
        self.assertEqual(d['consent'], 'pending'); self.assertEqual(d['segment'], 'new'); self.assertFalse(d['contactable'])
        self.assertEqual(d['phone_masked'], '+91 98••• ••210')
        w(core.confirm_consent, cid)
        with self.assertRaises(core.ApiError): w(core.confirm_consent, cid)
        sid = w(core.simulate_join)
        with db.reading() as c: self.assertEqual(core.customer_detail(c, sid)['consent'], 'opted_in')
        self.assertEqual(state()['live']['metrics']['total_customers'], 202)
        w(core.remove_customer, cid)
        self.assertEqual(state()['live']['metrics']['total_customers'], 201)

    def test_direct_send_and_undo(self):
        with db.reading() as c:
            lst = core.list_customers(c, segment='active_regular', consent='opted_in', page_size=5)
            none = core.list_customers(c, consent='none', page_size=2)
        ids = [x['id'] for x in lst['items']] + [x['id'] for x in none['items']]
        r = w(core.send_direct, ids)
        self.assertEqual(len(r['message_ids']), 5); self.assertEqual(len(r['skipped']), 2)
        with self.assertRaises(core.ApiError): w(core.send_direct, ids[:5])  # 30-day limit
        self.assertEqual(w(core.undo_direct, r['message_ids']), 5)
        w(core.send_direct, ids[:5])

    def test_profile(self):
        w(core.update_profile, {'closing_time': '22:00'})
        L = state()['live']
        self.assertEqual(L['briefing']['briefing_time_label'], '9:30 PM')
        with self.assertRaises(core.ApiError): w(core.update_profile, {'closing_time': '07:00'})
        with self.assertRaises(core.ApiError): w(core.update_profile, {'store_name': '  '})
        with self.assertRaises(core.ApiError): w(core.update_profile, {'languages': []})
        w(core.update_profile, {'day_off': 'tuesday'})
        sd = state()['live']['opportunities']['slow_day']
        self.assertNotEqual(sd['day'], 'Tuesday'); self.assertEqual(sd['excluded_day_off'], 'Tuesday')
        w(core.update_profile, {'discount_cap': 30})
        self.assertEqual(state()['live']['briefing']['discount_per_customer'], 30)
        w(core.reset_learning)
        L = state()['live']
        self.assertEqual(L['learned']['expected_return_rate'], 0.30); self.assertTrue(state()['frontend']['profile']['learnedReset'])

    def test_autopilot(self):
        w(core.update_profile, {'autonomy': 'auto'})
        w(core.advance, 7)
        self.assertEqual(state()['live']['briefing']['status'], 'running')

    def test_list_filters(self):
        with db.reading() as c:
            a = core.list_customers(c, q='priya')
            p = core.list_customers(c, page=2, page_size=25, sort='visits', order='asc')
            s = core.list_customers(c, segment='seasonal')
        self.assertTrue(all('priya' in x['name'].lower() for x in a['items']))
        self.assertEqual(p['page'], 2); self.assertEqual(len(p['items']), 25)
        self.assertEqual(s['total'], 12); self.assertTrue(all(not x['contactable'] for x in s['items']))
        with self.assertRaises(core.ApiError):
            with db.reading() as c: core.list_customers(c, segment='bogus')

    def test_quiet_hours(self):
        w(core.update_profile, {'quiet_hours_start': '20:00'})
        w(core.start_experiment)
        st = state()['live']['running']['offer_messages'][0]
        self.assertEqual(st['status'], 'queued'); self.assertTrue(st['created_at'].endswith('09:00:00'))

if __name__ == '__main__':
    unittest.main(verbosity=2)

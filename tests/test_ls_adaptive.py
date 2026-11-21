import unittest
from companion.services.ls_adaptive import AdaptivePolicy, Window


class AdaptivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = AdaptivePolicy(30,100,100,15)
        self.sequence = 0

    def step(self, count=1, *, ratio=1., rate=100., median=10.):
        for _ in range(count):
            self.sequence += 1
            start=self.sequence*2000.
            self.window = Window(self.sequence,start,start+2000,median,median*ratio,199,10 if ratio>1.3 else 0,rate)
            result=self.policy.observe(self.window,readback_verified=True)
        return result

    def test_uniform_slowdown_does_not_rebaseline_itself_healthy(self):
        self.step(4)
        self.assertEqual(self.step(12,median=16.) .state,"cadence_unverified")
        self.assertEqual(self.policy.current,100)

    def test_shadow_evaluator_never_changes_cap_or_extra_reserve(self):
        self.policy.shadow_only=True
        decision=self.step(20,ratio=1.7)
        self.assertEqual(decision.state,"shadow_trial")
        self.assertIsNone(decision.limit)
        self.assertEqual((self.policy.current,self.policy.extra),(100,0))

    def test_minimum_and_reserve_bounds_report_exhaustion(self):
        self.policy=AdaptivePolicy(100,100,100,15)
        self.assertEqual(self.step(3,ratio=1.7).state,"minimum_reached")
        self.policy=AdaptivePolicy(30,100,100,15)
        self.policy.extra=10
        self.assertEqual(self.step(3,ratio=1.7).state,"reserve_exhausted")

    def test_sustained_stability_allows_slow_upward_recovery(self):
        self.policy=AdaptivePolicy(30,100,90,15)
        self.policy.extra=4
        self.step(3,rate=90)
        self.assertEqual(self.step(30,rate=90).state,"recovering")
        self.assertEqual(self.policy.current,92)
        self.assertEqual(self.step(5,rate=92).state,"recovered")
        self.assertEqual((self.policy.current,self.policy.extra),(92,2))
        self.assertEqual(self.step(20,rate=92).state,"stable")
        self.assertEqual(self.policy.current,92)

    def test_upward_regression_rolls_back_and_has_a_dwell_period(self):
        self.policy=AdaptivePolicy(30,100,90,15)
        self.step(33,rate=90)
        self.assertEqual(self.policy.current,92)
        result=self.step(5,ratio=1.7,rate=92)
        self.assertEqual(result.state,"no_response")
        self.assertEqual(result.limit,90)
        self.assertEqual(self.step(3,ratio=1.7,rate=90).state,"cooldown")

    def test_trial_without_new_evidence_times_out_without_another_reduction(self):
        self.step(3,ratio=1.7)
        self.sequence += 15
        decision=self.step(ratio=1.7)
        self.assertEqual((decision.state,decision.limit),("no_response",100))

    def test_repeated_unhelpful_trials_stop_for_session(self):
        self.step(3,ratio=1.7)
        self.assertIsNotNone(self.policy.trial)
        self.step(5,ratio=1.7,rate=98)
        self.step(30,ratio=1.7)
        self.assertIsNotNone(self.policy.trial)
        self.step(5,ratio=1.7,rate=98)
        self.assertEqual(self.step(3,ratio=1.7).state,"no_response")
        self.assertIsNone(self.policy.trial)
        self.assertEqual(self.policy.current,100)

    def test_overlapping_windows_cancel_unconfirmed_trial(self):
        self.step(3,ratio=1.7)
        self.sequence -= 1
        result=self.step(ratio=1.7)
        self.assertEqual((result.state,result.limit),("unavailable",100))

    def test_window_validation_rejects_insufficient_nonfinite_or_out_of_range_data(self):
        payload={"sequence":1,"startedMs":1.,"endedMs":2001.,"medianMs":10.,"p95Ms":12.,"intervals":199,"relativeStalls":0}
        self.assertEqual(Window.parse(payload).sequence,1)
        for key,value in (("intervals",0),("intervals",2049),("p95Ms",float("nan")),("endedMs",1000),("medianMs",0),("relativeStalls",200)):
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                Window.parse({**payload,key:value})


if __name__ == "__main__":unittest.main()

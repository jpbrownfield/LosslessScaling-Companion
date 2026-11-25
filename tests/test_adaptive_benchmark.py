import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from companion.services.adaptive_benchmark import capture_metrics, summarize_overhead, summarize_adaptive, presentmon_trial_evidence, monitoring_comparison, cap_response_check
from companion.services.benchmark_metrics import summarize_presentmon_rows


def phase(enabled, *, ms=10., cap=True):
    metrics = summarize_presentmon_rows([{"Application":"LosslessScaling.exe","ProcessID":"200","FrameTime":str(ms),"DisplayedTime":str(ms)}]*200)
    metrics.update(span_seconds=19,swapchain_verified=True)
    return {"enabled":enabled,"seconds":20.,"control_confirmed":True,"fixed_cap_verified":cap,"ls":metrics,"source":metrics}


class AdaptiveBenchmarkTests(unittest.TestCase):
    def test_fixed_cap_abba_accepts_small_overhead_and_rejects_p99_regression(self):
        phases=[phase(False),phase(True),phase(True),phase(False)]
        self.assertEqual(summarize_overhead(phases)["status"],"pass")
        phases[1]["ls"]["metrics"]["frame_time_ms"]["p99"]=11
        self.assertEqual(summarize_overhead(phases)["status"],"fail")

    def test_missing_frame_coverage_drift_or_cap_change_cannot_pass(self):
        phases=[phase(False),phase(True),phase(True),phase(False)]
        phases[0]["ls"]["span_seconds"]=3
        self.assertEqual(summarize_overhead(phases)["status"],"inconclusive")
        phases=[phase(False),phase(True,cap=False),phase(True),phase(False)]
        self.assertEqual(summarize_overhead(phases)["status"],"fail")
        phases=[phase(False),phase(True,ms=12),phase(True),phase(False)]
        self.assertEqual(summarize_overhead(phases)["status"],"inconclusive")

    def test_presentmon_milliseconds_warmup_and_output_swapchain_are_respected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"p.csv"
            path.write_text("ProcessID,SwapChainAddress,CPUStartTime,FrameTime\n200,0xabc,0,500\n200,0xabc,1000,20\n200,0xabc,2000,10\n200,0xabc,3000,10\n200,0xdef,3000,999\n300,0xabc,3000,999\n")
            result=capture_metrics([path],200,0xabc)
            self.assertEqual(result["rows"],2)
            self.assertEqual(result["metrics"]["frame_time_ms"]["mean"],10.)
            self.assertEqual(result["span_seconds"],3)

    def test_native_success_and_headroom_alone_cannot_pass_acceptance(self):
        samples=[{"elapsed_seconds":2*i,"state":"stable","gpu_headroom_percent":95,
                  "benefit_confirmed":True,"comparison":{"rateResponseObserved":True},
                  "pacing":{"sequence":i,"generation":1,"identityVerified":True,"ageMs":0}}
                 for i in range(1,11)]
        # No independently aligned ETW trial, so benefit/enforcement stay unknown.
        result=summarize_adaptive(samples,duration=20,warmup=0,overhead={"status":"pass"})
        self.assertEqual(result["status"],"inconclusive")
        self.assertEqual(result["ls_benefit"]["status"],"inconclusive")
        self.assertEqual(result["cap_enforcement"]["status"],"inconclusive")

    def test_rejected_trial_or_failed_actuator_cannot_produce_overall_pass(self):
        samples=[{"elapsed_seconds":2*i,"state":"stable","benefit_confirmed":True,
                  "pacing":{"sequence":i,"generation":1,"identityVerified":True,"ageMs":0}}
                 for i in range(1,11)]
        with patch("companion.services.adaptive_benchmark.presentmon_trial_evidence",return_value=[{"status":"pass","nativeAccepted":False,"cap_enforced":True}]):
            self.assertEqual(summarize_adaptive(samples,duration=20,warmup=0,overhead={"status":"pass"})["status"],"inconclusive")
        with patch("companion.services.adaptive_benchmark.presentmon_trial_evidence",return_value=[{"status":"pass","nativeAccepted":True,"cap_enforced":True}]):
            self.assertEqual(summarize_adaptive(samples,duration=20,warmup=0,overhead={"status":"pass"},cap_response={"status":"fail"})["status"],"fail")

    def test_qpc_aligned_etw_verifies_actuator_and_ls_benefit_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"p.csv"
            lines=["ProcessID,SwapChainAddress,CPUStartQPCTime,FrameTime,DisplayedTime"]
            for start,cap,ls_time in ((10000,100,18.),(20000,98,10.)):
                for i in range(200):
                    tick=start+i*10
                    lines.append(f"100,0x1,{tick},{1000/cap},10")
                    interval=ls_time if i%5==0 else 10.
                    lines.append(f"200,0xabc,{tick},{interval},{interval}")
            path.write_text("\n".join(lines))
            decision={"pacing":{"swapchain":0xabc},"comparison":{"beforeWindows":[[10000,12000]],"afterWindows":[[20000,22000]],"previousLimit":100,"proposedLimit":98}}
            evidence=presentmon_trial_evidence([path],100,200,[decision])[0]
            self.assertTrue(evidence["cap_enforced"])
            self.assertTrue(evidence["ls_relative_benefit"])
            self.assertEqual(evidence["status"],"pass")
            lines=[line.replace(",10.204081632653061,",",10.0,") for line in lines]
            path.write_text("\n".join(lines))
            evidence=presentmon_trial_evidence([path],100,200,[decision])[0]
            self.assertFalse(evidence["cap_enforced"])
            self.assertNotEqual(evidence["status"],"pass")

    def test_short_burst_or_overlapping_windows_cannot_pass_independent_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"p.csv"
            lines = ["ProcessID,SwapChainAddress,CPUStartQPCTime,FrameTime,DisplayedTime"]
            for start in (10000, 20000):
                for index in range(120):
                    lines.extend((f"100,0x1,{start+index},10,10", f"200,0xabc,{start+index},10,10"))
            path.write_text("\n".join(lines))
            decision = {"pacing": {"swapchain": 0xabc}, "benefit_confirmed": True,
                        "comparison": {"beforeWindows": [[10000, 12000]], "afterWindows": [[20000, 22000]],
                                       "previousLimit": 100, "proposedLimit": 98}}
            self.assertEqual(presentmon_trial_evidence([path],100,200,[decision])[0]["status"], "inconclusive")
            decision["comparison"]["afterWindows"] = [[11000, 13000]]
            self.assertIn("overlapping", presentmon_trial_evidence([path],100,200,[decision])[0]["reason"])

    def test_controlled_cap_step_verifies_source_rate_and_restores_fixed_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"p.csv"
            path.write_text("ProcessID,CPUStartTime,FrameTime\n"+"\n".join(f"100,{i*10},{1000/90}" for i in range(1500)))
            rtss=Mock();limits=[]
            rtss.apply_profile.side_effect=lambda *_,**kw: limits.append(kw["framerate_limit"])
            rtss.read_limit.side_effect=lambda _: limits[-1]
            with patch("companion.services.adaptive_benchmark.PresentMonCapture") as capture:
                capture.return_value.csv_files.return_value=[path]
                result=cap_response_check(rtss=rtss,profile=Mock(),presentmon=Path("p.exe"),directory=folder,process_names=["game.exe"],source_pid=100,fixed_cap=100,minimum=30,baseline_phases=[phase(False)])
            self.assertEqual(result["status"],"pass")
            self.assertEqual(limits,[90,100])

    def test_controlled_cap_step_restores_rtss_when_capture_fails(self):
        rtss=Mock();limits=[]
        rtss.apply_profile.side_effect=lambda *_,**kw: limits.append(kw["framerate_limit"])
        rtss.read_limit.side_effect=lambda _: limits[-1]
        with tempfile.TemporaryDirectory() as folder, patch("companion.services.adaptive_benchmark.PresentMonCapture") as capture:
            capture.return_value.wait.side_effect=RuntimeError("ETW failed")
            with self.assertRaisesRegex(RuntimeError,"ETW failed"):
                cap_response_check(rtss=rtss,profile=Mock(),presentmon=Path("p.exe"),directory=folder,process_names=["game.exe"],source_pid=100,fixed_cap=100,minimum=30,baseline_phases=[])
        self.assertEqual(limits,[90,100])
        capture.return_value.stop.assert_called_once()

    def test_monitor_control_is_restored_when_capture_raises(self):
        host={"presentationPacing":{"enabled":False,"identityVerified":True,"swapchain":123}}
        with tempfile.TemporaryDirectory() as folder, patch("companion.services.adaptive_benchmark.presentation_status",return_value=host) as status, patch("companion.services.adaptive_benchmark.PresentMonCapture") as capture:
            capture.return_value.start.side_effect=RuntimeError("capture failed")
            with self.assertRaisesRegex(RuntimeError,"capture failed"):
                monitoring_comparison(executable="ls.exe",instance=(200,1.),binding=(100,1,2,3),presentmon=Path("p.exe"),directory=folder,source_pid=100,process_names=["game.exe"],fixed_cap=100,rtss=Mock(),profile=Mock())
            self.assertEqual(status.call_args.kwargs,{"enabled":False})
            capture.return_value.stop.assert_called_once()


if __name__ == "__main__":unittest.main()

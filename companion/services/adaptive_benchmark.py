"""Acceptance measurements shared by the existing benchmark and application tests.

ABBA keeps RTSS fixed. PresentMon stays enabled in both arms. It measures the
incremental observer cost, including polling/sorting, without adaptive writes.
"""
from __future__ import annotations

import statistics
import math
import time
from pathlib import Path

from .addon_host import presentation_status
from .benchmark_metrics import read_presentmon_csv, summarize_presentmon_rows, presentmon_time_seconds
from .performance_benchmark import PresentMonCapture


def _chain(value):
    try:
        return int(str(value), 16)  # PresentMon SwapChainAddress uses hex
    except (ValueError, TypeError):
        return None


def capture_metrics(paths, pid, chain=None, warmup=2.):
    rows = [row for path in paths for row in read_presentmon_csv(path)
            if str(row.get("ProcessID", "")).strip() == str(pid)
            and (chain is None or _chain(row.get("SwapChainAddress")) == chain)]
    summary = summarize_presentmon_rows(rows, warmup_seconds=warmup)
    times = [value for row in rows for value in [presentmon_time_seconds(row)] if value is not None]
    summary["span_seconds"] = max(times)-min(times) if times else 0
    summary["swapchain_verified"] = chain is not None and bool(rows)
    return summary


def summarize_overhead(phases):
    result = {"status": "inconclusive", "method": "fixed-cap ABBA, separate PresentMon sessions",
              "p95_budget_ms": "max(0.10 ms, 2%)", "p99_budget_ms": "max(0.20 ms, 3%)",
              "phases": phases, "absolute_delivery_verified": False}
    if len(phases) != 4 or [p.get("enabled") for p in phases] != [False, True, True, False]:
        return {**result, "reason": "Incomplete ABBA comparison"}
    if any(not p.get("control_confirmed") or not p.get("fixed_cap_verified") for p in phases):
        return {**result, "status": "fail", "reason": "Monitoring control or fixed RTSS cap was not confirmed"}
    if any(not p.get("ls", {}).get("swapchain_verified") or p["ls"]["rows"] < 120
           or p["ls"]["span_seconds"] < p["seconds"]-4 for p in phases):
        return {**result, "reason": "Missing full-duration PresentMon evidence for the bound LS swapchain"}
    if any(p.get("source", {}).get("rows", 0) < 120 or p["source"].get("span_seconds", 0) < p["seconds"]-4 for p in phases):
        return {**result, "reason": "Missing full-duration source PresentMon evidence"}
    off = [p for p in phases if not p["enabled"]]; on = [p for p in phases if p["enabled"]]
    def metric(phase, group, name, key):
        return phase[group]["metrics"][name][key]
    if any(type(metric(p,"ls","frame_time_ms",key)) not in (int,float)
           or not math.isfinite(metric(p,"ls","frame_time_ms",key)) or metric(p,"ls","frame_time_ms",key) <= 0
           for p in phases for key in ("median", "p95", "p99")):
        return {**result, "reason": "LS percentile evidence is missing or invalid"}
    if any(metric(p, "source", "frame_time_ms", "mean") in (None, 0) for p in phases):
        return {**result, "reason": "Source-rate evidence is missing"}
    rates = [1000/metric(p,"source","frame_time_ms","mean") for p in phases]
    medians = [metric(p,"ls","frame_time_ms","median") for p in phases]
    if any(x in (None, 0) for x in medians):
        return {**result, "reason": "LS timing evidence is missing"}
    if max(rates)/min(rates) > 1.03 or max(medians)/min(medians) > 1.05:
        return {**result, "reason": "Cadence drift makes the overhead comparison inconclusive"}
    comparisons = {}
    for percentile, absolute, relative in (("p95", .10, .02), ("p99", .20, .03)):
        baseline = statistics.mean(metric(p,"ls","frame_time_ms",percentile) for p in off)
        monitored = statistics.mean(metric(p,"ls","frame_time_ms",percentile) for p in on)
        budget = max(absolute, baseline*relative)
        comparisons[percentile] = {"off_ms": baseline, "on_ms": monitored, "delta_ms": monitored-baseline,
                                   "budget_ms": budget, "within_budget": monitored-baseline <= budget}
    # Displayed-frame evidence, when available, must also stay within budget.
    for percentile, absolute, relative in (("p95", .10, .02), ("p99", .20, .03)):
        values = [metric(p,"ls","displayed_time_ms",percentile) for p in phases]
        if all(x is not None and x > 0 for x in values):
            baseline = (values[0]+values[3])/2;monitored=(values[1]+values[2])/2
            comparisons["displayed_"+percentile] = {"off_ms": baseline, "on_ms": monitored,
                "delta_ms": monitored-baseline, "budget_ms": max(absolute,baseline*relative),
                "within_budget": monitored-baseline <= max(absolute,baseline*relative)}
    passed = all(v["within_budget"] for v in comparisons.values())
    return {**result, "status": "pass" if passed else "fail", "comparisons": comparisons,
            "reason": "Measured incremental monitoring cost is within budget" if passed else "Monitoring exceeded the frametime budget"}


def monitoring_comparison(*, executable, instance, binding, presentmon, directory, source_pid,
                          process_names, fixed_cap, rtss, profile, phase_seconds=20.,
                          cancelled=lambda: False, sample_callback=None, expected_luid=None):
    phases = []
    previous_enabled = None
    try:
        initial = presentation_status(executable, instance, binding=binding)
        previous_enabled = initial["presentationPacing"]["enabled"]
        presentation_status(executable, instance, enabled=True)
        deadline = time.monotonic()+8
        chain = None
        while time.monotonic() < deadline:
            host = presentation_status(executable, instance, binding=binding)
            pacing = host["presentationPacing"]
            if pacing.get("identityVerified") and pacing.get("swapchain"):
                chain = pacing["swapchain"];break
            if cancelled():
                raise RuntimeError("Benchmark interrupted or source process exited")
            time.sleep(.25)
        if not chain:
            return {"status": "inconclusive", "reason": "The native LS output identity could not be bound", "phases": []}
        capture_keys=("adapterLuid", "captureApi", "frameGeneration", "scaler", "hdr", "width", "height", "settingsAppliedTick")
        monitor_keys=("monitor", "refreshHz", "monitorWidth", "monitorHeight", "sourcePid", "sourceWindow", "outputWindow", "context")
        capture_context={key:host.get("capture",{}).get(key) for key in capture_keys}
        monitor_context={key:pacing.get(key) for key in monitor_keys}
        for index, enabled in enumerate((False, True, True, False)):
            if cancelled():
                raise RuntimeError("Benchmark interrupted or source process exited")
            presentation_status(executable, instance, enabled=enabled)
            phase = {"enabled": enabled, "seconds": phase_seconds, "fixed_cap": fixed_cap,
                     "control_confirmed": True, "fixed_cap_verified": True, "samples": []}
            capture = PresentMonCapture(presentmon, Path(directory)/f"monitoring-{index}")
            try:
                capture.start(process_names, phase_seconds)
                deadline = time.monotonic()+phase_seconds
                while time.monotonic() < deadline:
                    if cancelled():
                        raise RuntimeError("Benchmark interrupted or source process exited")
                    host = presentation_status(executable, instance, binding=binding)
                    pacing = host["presentationPacing"]
                    phase["control_confirmed"] &= pacing.get("enabled") is enabled and pacing.get("identityVerified") is True
                    phase["fixed_cap_verified"] &= rtss.read_limit(profile) == fixed_cap
                    phase["control_confirmed"] &= all(host.get("capture",{}).get(k)==v for k,v in capture_context.items()) and all(pacing.get(k)==v for k,v in monitor_context.items())
                    if enabled and pacing.get("swapchain") not in (0, chain):
                        phase["control_confirmed"] = False
                    if enabled and expected_luid:
                        from .dynamic_limiter import DynamicLimiterController
                        phase["control_confirmed"] &= DynamicLimiterController._luid(pacing.get("presentationLuid")) == DynamicLimiterController._luid(expected_luid)
                    phase["samples"].append({"pacing": pacing, "capture": host.get("capture"), "sampledAt": host.get("sampledAt")})
                    if sample_callback:
                        sample_callback(index, enabled, pacing)
                    time.sleep(min(1., max(.01, deadline-time.monotonic())))
                capture.wait(timeout=15)
                files = capture.csv_files()
                phase["ls"] = capture_metrics(files, instance[0], chain)
                phase["source"] = capture_metrics(files, source_pid)
            finally:
                capture.stop()
            phases.append(phase)
        return summarize_overhead(phases)
    finally:
        if previous_enabled is not None:
            presentation_status(executable, instance, enabled=previous_enabled)


def cap_response_check(*, rtss, profile, presentmon, directory, process_names, source_pid,
                       fixed_cap, minimum, baseline_phases, seconds=15.):
    """An isolated actuator check, separate from the LS-benefit objective."""
    trial = max(minimum, int(fixed_cap*.9))
    if trial == fixed_cap:
        return {"status": "inconclusive", "reason": "FPS bounds leave no room for an actuator test"}
    capture = PresentMonCapture(presentmon, Path(directory)/"cap-response")
    try:
        rtss.apply_profile(profile, profile, framerate_limit=trial)
        if rtss.read_limit(profile) != trial:
            raise RuntimeError("Controlled RTSS test cap readback failed")
        capture.start(process_names, seconds)
        capture.wait(timeout=seconds+15)
        source = capture_metrics(capture.csv_files(), source_pid)
        baseline = [p.get("source",{}).get("metrics",{}).get("frame_time_ms",{}).get("mean") for p in baseline_phases]
        mean = source["metrics"]["frame_time_ms"]["mean"]
        if not mean or not baseline or any(x is None or x <= 0 for x in baseline) or source["span_seconds"] < seconds-4:
            return {"status":"inconclusive", "reason":"Insufficient source-rate ETW evidence"}
        before, after = statistics.mean(1000/x for x in baseline), 1000/mean
        was_limited = abs(before-fixed_cap) <= max(1., fixed_cap*.03)
        matched = abs(after-trial) <= max(1.,trial*.03) and before-after >= (fixed_cap-trial)*.4
        return {"status": "pass" if was_limited and matched else "fail" if was_limited else "inconclusive",
                "reason": "Independent source-rate response" if was_limited and matched else "The workload was not cap-bound or the measured source rate did not follow RTSS",
                "initial_cap": fixed_cap, "trial_cap": trial, "before_fps":before, "after_fps":after,
                "scope":"Source PresentMon rates; separate from LS timing quality"}
    finally:
        try:
            capture.stop()
        finally:
            rtss.apply_profile(profile, profile, framerate_limit=fixed_cap)
            if rtss.read_limit(profile) != fixed_cap:
                raise RuntimeError("Could not restore the fixed RTSS cap after actuator test")


def presentmon_trial_evidence(paths, source_pid, ls_pid, decisions):
    # --qpc_time_ms puts native completed windows and ETW frames on the same
    # clock. Never align by file creation time or an assumed LSFG multiplier.
    rows = [row for path in paths for row in read_presentmon_csv(path)]
    results = []
    def selected(pid, chain, spans):
        chosen = []
        stamps = [[] for _ in spans]
        for row in rows:
            if str(row.get("ProcessID", "")).strip() != str(pid):
                continue
            if chain is not None and _chain(row.get("SwapChainAddress")) != chain:
                continue
            try:
                stamp = float(row["CPUStartQPCTime"])
            except (ValueError, KeyError):
                continue
            if not math.isfinite(stamp):
                continue
            for index, (start, end) in enumerate(spans):
                if start <= stamp < end:
                    chosen.append(row);stamps[index].append(stamp);break
        summary = summarize_presentmon_rows(chosen)
        # A burst of many rows cannot stand in for an entire native window.
        # Require coverage of every before/after span, including its boundaries.
        coverage = []
        for (start, end), times in zip(spans, stamps):
            times = sorted(set(times))
            span = end-start
            gaps = [b-a for a,b in zip(times, times[1:])]
            coverage.append(bool(len(times) >= 30 and (times[-1]-times[0])/span >= .8
                                 and times[0]-start <= span*.1 and end-times[-1] <= span*.1
                                 and gaps and max(gaps) <= max(250., statistics.median(gaps)*10)))
        summary["aligned_window_coverage"] = bool(coverage) and all(coverage)
        return summary
    for decision in decisions:
        comparison = decision.get("comparison") or {}
        if not comparison:
            continue
        before, after = comparison.get("beforeWindows", []), comparison.get("afterWindows", [])
        chain = (decision.get("pacing") or {}).get("swapchain")
        evidence = {"status": "inconclusive", "comparison": comparison, "swapchain": chain, "nativeAccepted": decision.get("benefit_confirmed") is True}
        def valid(spans):
            return bool(spans) and all(isinstance(pair, (list, tuple)) and len(pair) == 2
                and all(type(x) in (int, float) and math.isfinite(x) for x in pair)
                and 1800 <= pair[1]-pair[0] <= 2500 for pair in spans) and all(a[1] <= b[0] for a,b in zip(spans,spans[1:]))
        if not valid(before) or not valid(after) or before[-1][1] > after[0][0]:
            results.append({**evidence, "reason": "Invalid or overlapping native comparison windows"});continue
        source_before = selected(source_pid, None, before)
        source_after = selected(source_pid, None, after)
        ls_before = selected(ls_pid, chain, before)
        ls_after = selected(ls_pid, chain, after)
        if not chain or any(s["rows"] < 60 or not s["aligned_window_coverage"] for s in (source_before,source_after,ls_before,ls_after)):
            results.append({**evidence, "reason": "Missing aligned source/LS swapchain ETW evidence"});continue
        mean_before = source_before["metrics"]["frame_time_ms"]["mean"]
        mean_after = source_after["metrics"]["frame_time_ms"]["mean"]
        if not mean_before or not mean_after:
            results.append({**evidence, "reason": "No source frame interval evidence"});continue
        before_fps, after_fps = 1000/mean_before, 1000/mean_after
        old, new = comparison["previousLimit"], comparison["proposedLimit"]
        enforced = (abs(before_fps-old) <= max(1., old*.03)
                    and abs(after_fps-new) <= max(1., new*.03)
                    and (after_fps-before_fps)*(new-old) > 0
                    and abs(after_fps-before_fps) >= abs(new-old)*.4)
        bm, am = ls_before["metrics"], ls_after["metrics"]
        before_time, after_time = bm["frame_time_ms"], am["frame_time_ms"]
        jitter_before = before_time["p95"]/before_time["median"]-1 if before_time["median"] else None
        jitter_after = after_time["p95"]/after_time["median"]-1 if after_time["median"] else None
        relative_benefit = jitter_before is not None and jitter_after is not None and jitter_before-jitter_after >= max(.10,jitter_before*.2)
        display_before, display_after = bm["displayed_time_ms"], am["displayed_time_ms"]
        display_available = display_before["count"] >= 60 and display_after["count"] >= 60
        display_safe = bool(display_available and display_after["p95"] <= display_before["p95"]*1.05
                            and display_after["mean"] <= display_before["mean"]*1.10)
        evidence.update(source_before_fps=before_fps, source_after_fps=after_fps,
                        cap_enforced=enforced, ls_relative_benefit=relative_benefit,
                        displayed_evidence_available=display_available, displayed_timing_preserved=display_safe,
                        ls_before=ls_before, ls_after=ls_after)
        evidence["status"] = "pass" if enforced and relative_benefit and display_safe else "inconclusive" if not display_available else "fail"
        results.append(evidence)
    return results


def summarize_adaptive(samples, *, duration, warmup, overhead=None, csv_paths=(), source_pid=None, ls_pid=None, cap_response=None):
    selected = [row for row in samples if row.get("elapsed_seconds", 0) >= warmup]
    windows = {}
    for row in selected:
        p = row.get("pacing") or {}
        if row.get("state") not in ("unavailable", "error") and p.get("sequence") and p.get("identityVerified") and p.get("ageMs", 1e9) <= 3000:
            windows[(p.get("generation"), p["sequence"])] = row
    unavailable = [row for row in selected if row.get("state") in ("unavailable", "error")]
    coverage = len(windows)/max(1., (duration-warmup)/2)
    telemetry = {"status": "pass" if coverage >= .8 and len(unavailable) <= .1*max(1,len(selected)) else "fail",
                 "window_count": len(windows), "window_coverage": coverage, "unavailable_samples": len(unavailable)}
    decisions = [row for row in selected if row.get("benefit_confirmed") is not None]
    comparisons = [row.get("comparison", {}) for row in decisions if row.get("comparison")]
    etw = presentmon_trial_evidence(csv_paths, source_pid, ls_pid, decisions)
    benefits = [row for row in etw if row.get("status") == "pass" and row.get("nativeAccepted")]
    effects = [row for row in etw if row.get("cap_enforced")]
    cap_failed = (cap_response or {}).get("status") == "fail"
    confirmation_failed = any(row.get("nativeAccepted") and row.get("status") == "fail" for row in etw)
    incomplete_confirmation = any(row.get("nativeAccepted") and row.get("status") == "inconclusive" for row in etw)
    if cap_failed or confirmation_failed or telemetry["status"] == "fail" or (overhead or {}).get("status") == "fail":
        status = "fail"
    elif benefits and not incomplete_confirmation and (overhead or {}).get("status") == "pass":
        status = "pass"
    else:
        status = "inconclusive"
    return {"policy_version": 2, "status": status,
            "telemetry_validity": telemetry,
            "cap_enforcement": {"status": "fail" if cap_failed else "pass" if effects or (cap_response or {}).get("status") == "pass" else "inconclusive",
                                "controlled_step": cap_response,
                                "evidence": "QPC-aligned source PresentMon rates plus stored RTSS readback",
                                "responses": len(effects), "presentmon_runtime_enforcement_verified": bool(effects) or (cap_response or {}).get("status") == "pass"},
            "ls_benefit": {"status": "fail" if confirmation_failed else "pass" if benefits else "inconclusive", "confirmed_trials": len(benefits), "comparisons": comparisons, "presentmon_trials": etw,
                           "reason": "No beneficial pressure trial observed" if not benefits else "Relative LS timing improved after a bounded source-cap trial"},
            "monitoring_overhead": overhead or {"status": "not_run"},
            "absolute_delivery_verified": False,
            "final_state": selected[-1].get("state") if selected else None}

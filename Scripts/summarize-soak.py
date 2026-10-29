"""Summarize benchmark-soak.py JSONL, including between-round media and I/O."""
import argparse
import json
import math
import statistics
from pathlib import Path


def quantile(values, q):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered)-1, math.ceil(q*len(ordered))-1))]


def summarize(root):
    rounds = [json.loads(line) for line in (root/'rounds.jsonl').read_text().splitlines() if line]
    samples = [json.loads(line) for line in (root/'process-samples.jsonl').read_text().splitlines() if line]
    result = dict(experiment=json.loads((root/'experiment.json').read_text()),phases={})
    for phase in dict.fromkeys(row['phase'] for row in rounds):
        rows = [row for row in rounds if row['phase']==phase]
        measured = [row for row in samples if row['phase']==phase]
        totals = dict(seconds=0.0,cpu_seconds=0.0,read_bytes=0,write_bytes=0,logical_write_bytes=0)
        for a,b in zip(samples,samples[1:]):
            if a['phase']!=phase or b['phase']!=phase:
                continue
            totals['seconds'] += b['at_monotonic']-a['at_monotonic']
            for key in ('cpu_seconds','read_bytes','write_bytes','logical_write_bytes'):
                totals[key] += b[key]-a[key]
        elapsed = max(totals['seconds'],.001)
        values = [r['duration_seconds'] for r in rows]
        memory = [r['rss_bytes'] for r in measured]
        third = max(1,len(memory)//3)
        result['phases'][phase] = dict(rounds=len(rows),successful=sum(r['ok'] for r in rows),
            overran_interval=sum(r['overran_interval'] for r in rows),
            round_seconds_median=statistics.median(values),round_seconds_p95=quantile(values,.95),
            round_seconds_max=max(values),round_cpu_seconds_median=statistics.median(r['cpu_seconds'] for r in rows),
            whole_process_cpu_percent_one_core=100*totals['cpu_seconds']/elapsed,
            whole_process_read_bytes_per_10s=10*totals['read_bytes']/elapsed,
            whole_process_write_bytes_per_10s=10*totals['write_bytes']/elapsed,
            whole_process_logical_write_bytes_per_10s=10*totals['logical_write_bytes']/elapsed,
            rate_window_seconds=elapsed,rate_is_partial=elapsed<10,
            rss_sample_max=max(memory),rss_sample_first_third_median=statistics.median(memory[:third]),
            rss_sample_last_third_median=statistics.median(memory[-third:]),
            rss_first_to_last_median_delta=statistics.median(memory[-third:])-statistics.median(memory[:third]),
            footprint_sample_max=max(r['footprint_bytes'] for r in measured),
            fds_min=min(r['fds'] for r in measured),fds_max=max(r['fds'] for r in measured),
            fds_last=measured[-1]['fds'],threads_min=min(r['threads'] for r in measured),threads_max=max(r['threads'] for r in measured),
            text_pending_max=max(r['text_pending'] for r in rows),text_pending_last=rows[-1]['text_pending'],
            media_pending_max=max(r['media_pending'] for r in rows),media_pending_last=rows[-1]['media_pending'],
            media_ready_first=rows[0]['media_ready'],media_ready_last=rows[-1]['media_ready'],
            media_failed_max=max(r['media_failed'] for r in rows),
            scheduler_lag_seconds_max=max(r['scheduler_lag_seconds'] for r in rows),totals=totals)
    if (root/'final.json').exists():
        result['final']=json.loads((root/'final.json').read_text())
    result['measurement_notes']=[
        'CPU percent is relative to one core (100%=one fully occupied core), includes sampler and background media.',
        'Disk is macOS proc_pid_rusage V4 process-attributed I/O. Cached reads may be zero; delayed writes may occur between rounds. Not SSD NAND bytes or whole-machine I/O.',
        'Whole-process per-10s rates use 200ms samples over each phase; no producer/server CPU or I/O included.',
        'Intervals crossing phase boundaries are excluded to avoid charging the first busy round to the preceding idle phase; raw per-round counters remain available.',
        'Drain is a final partial cycle; its normalized rates are flagged partial and should not be interpreted as sustained load.',
        'Sampled phase RSS peaks can miss short spikes. Raw samples also retain OS lifetime peak RSS.',
        'Five-Hz sampler overhead is not subtracted; see sampling-control.json.',
        'Message pending is source-minus-index at measurement time, including normal arrivals during a sync.',
        'This is an isolated synthetic warm-cache service test; no installed settings or real WeChat credentials are changed.'
    ]
    (root/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    print(json.dumps(summarize(args.directory),indent=2))

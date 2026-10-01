#!/usr/bin/env python3
"""
XTOS Real-Time Test Results Analyzer
Parses cyclictest JSON output and generates statistical reports.
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Any
import statistics


def load_results(json_path: Path) -> Dict[str, Any]:
    """Load cyclictest JSON results."""
    with open(json_path, 'r') as f:
        data = json.load(f)
    
    if data.get('schema') != 'xtos-rt-cyclictest-v1':
        print(f"Warning: Unexpected schema: {data.get('schema')}", file=sys.stderr)
    
    return data


def analyze_thread(thread: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze single thread results."""
    latency = thread['latency_us']
    
    return {
        'thread_id': thread['thread_id'],
        'pid': thread['pid'],
        'cycles': thread['cycles'],
        'min_us': latency['min'],
        'avg_us': latency['avg'],
        'max_us': latency['max'],
        'range_us': latency['max'] - latency['min'],
    }


def analyze_system(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute system-wide statistics across all threads."""
    if not results:
        return {}
    
    all_max = [r['latency_us']['max'] for r in results]
    all_avg = [r['latency_us']['avg'] for r in results]
    all_min = [r['latency_us']['min'] for r in results]
    
    return {
        'worst_case_us': max(all_max),
        'best_case_us': min(all_min),
        'mean_avg_us': statistics.mean(all_avg),
        'total_cycles': sum(r['cycles'] for r in results),
    }


def generate_report(data: Dict[str, Any]) -> str:
    """Generate human-readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append("XTOS Real-Time Latency Test Report")
    lines.append("=" * 70)
    lines.append("")
    
    # System info
    lines.append("Test Configuration:")
    lines.append(f"  Timestamp:     {data['timestamp']}")
    lines.append(f"  Duration:      {data['duration_sec']}s ({data['duration_sec'] / 3600:.1f}h)")
    lines.append(f"  Priority:      {data['priority']} (SCHED_FIFO)")
    lines.append(f"  Threads:       {data['threads']}")
    lines.append(f"  Interval:      {data['interval_us']}us")
    lines.append("")
    
    system = data.get('system', {})
    lines.append("System:")
    lines.append(f"  Hostname:      {system.get('hostname', 'unknown')}")
    lines.append(f"  Kernel:        {system.get('kernel', 'unknown')}")
    lines.append(f"  CPU:           {system.get('cpu_model', 'unknown')}")
    lines.append("")
    
    # Per-thread results
    results = data.get('results', [])
    if not results:
        lines.append("Error: No results found")
        return "\n".join(lines)
    
    lines.append("Per-Thread Latency (microseconds):")
    lines.append("")
    lines.append("  Thread   PID      Cycles      Min      Avg      Max    Range")
    lines.append("  " + "-" * 64)
    
    for thread in results:
        analyzed = analyze_thread(thread)
        lines.append(
            f"  {analyzed['thread_id']:6d}   {analyzed['pid']:6d}  {analyzed['cycles']:9d}  "
            f"{analyzed['min_us']:7d}  {analyzed['avg_us']:7d}  {analyzed['max_us']:7d}  "
            f"{analyzed['range_us']:7d}"
        )
    
    lines.append("")
    
    # System-wide summary
    system_stats = analyze_system(results)
    lines.append("System-Wide Summary:")
    lines.append(f"  Worst-case latency:  {system_stats['worst_case_us']} us")
    lines.append(f"  Best-case latency:   {system_stats['best_case_us']} us")
    lines.append(f"  Mean average:        {system_stats['mean_avg_us']:.2f} us")
    lines.append(f"  Total cycles:        {system_stats['total_cycles']:,}")
    lines.append("")
    
    # Real-time classification
    worst = system_stats['worst_case_us']
    lines.append("Real-Time Classification:")
    if worst < 100:
        lines.append(f"  ✓ Hard real-time capable (max: {worst} us < 100 us)")
    elif worst < 1000:
        lines.append(f"  ~ Soft real-time (max: {worst} us < 1 ms)")
    else:
        lines.append(f"  ✗ Not suitable for real-time (max: {worst} us >= 1 ms)")
    
    lines.append("")
    lines.append("Notes:")
    lines.append("  - These results are environment-specific")
    lines.append("  - Virtualized systems show degraded RT performance")
    lines.append("  - Real hardware with proper tuning required for guarantees")
    lines.append("  - See rt_system_check.sh output for configuration recommendations")
    lines.append("")
    lines.append("=" * 70)
    
    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <cyclictest_results.json>", file=sys.stderr)
        print("", file=sys.stderr)
        print("Analyzes XTOS cyclictest JSON output and generates a report.", file=sys.stderr)
        sys.exit(1)
    
    json_path = Path(sys.argv[1])
    if not json_path.exists():
        print(f"Error: File not found: {json_path}", file=sys.stderr)
        sys.exit(1)
    
    try:
        data = load_results(json_path)
        report = generate_report(data)
        print(report)
        
        # Save report alongside JSON
        report_path = json_path.with_suffix('.report.txt')
        with open(report_path, 'w') as f:
            f.write(report)
        
        print(f"Report saved to: {report_path}", file=sys.stderr)
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

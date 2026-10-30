"""Executable fixed-partition CLI contract and no physical SMP claims."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools/xtos-sched-model/partitioned.py'


from unittest.mock import patch
sys.path.insert(0, str(ROOT / 'tools/xtos-sched-model'))
import partitioned


class PartitionedCliTests(unittest.TestCase):
    @staticmethod
    def payload():
        return {'schema':'xtos-scheduler-partitioned-input-v1', 'partition_cpus':[3, 1, 7],
                'horizon_ns':20, 'tasks':[
                    {'name':'a', 'cpu':1, 'runtime_ns':2, 'deadline_ns':10, 'period_ns':10},
                    {'name':'b', 'cpu':3, 'runtime_ns':3, 'deadline_ns':10, 'period_ns':10}]}

    def invoke(self, raw, *extra):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source=Path(directory.name)/'input.json'; output=Path(directory.name)/'report.json'
        source.write_bytes(raw)
        result=subprocess.run([sys.executable,str(CLI),'--input',str(source),'--output',str(output),
                               *extra],capture_output=True,text=True,timeout=10)
        self.assertEqual(source.read_bytes(),raw)
        return result,source,output

    def test_strict_json_and_partition_contract_reject_without_publication(self):
        import copy
        valid=self.payload()
        cases=[]
        for key,value in [('cpu',1),('schema','wrong'),('partition_cpus',[1,1]),
                          ('partition_cpus',[True,3]),('partition_cpus',[]),
                          ('horizon_ns',True),('horizon_ns',2**63),('tasks',[])]:
            item=copy.deepcopy(valid);item[key]=value;cases.append(json.dumps(item).encode())
        for key,value in [('cpu',9),('typo',1),('runtime_ns',1.5),('name','b'),
                          ('work_profile_ns',[1]),('work_ns',True)]:
            item=copy.deepcopy(valid);item['tasks'][0][key]=value;cases.append(json.dumps(item).encode())
        raw=json.dumps(valid)
        cases.extend([raw.replace('"horizon_ns": 20','"horizon_ns": 20, "horizon_ns": 10').encode(),
                      raw.replace('"horizon_ns": 20','"horizon_ns": NaN').encode(),
                      raw.replace('"horizon_ns": 20','"horizon_ns": 1e999').encode()])
        for raw in cases:
            with self.subTest(raw=raw):
                result,_,out=self.invoke(raw)
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertFalse(out.exists())

    def test_per_cpu_overload_is_diagnostic_not_total_capacity_admission(self):
        payload=self.payload()
        payload['tasks']=[{'name':name,'cpu':1,'runtime_ns':6,'deadline_ns':10,'period_ns':10}
                          for name in ('a','b')]
        result,_,out=self.invoke(json.dumps(payload).encode())
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(out.read_text())
        self.assertFalse(report['reservation_admission']['accepted'])
        self.assertEqual(report['reservation_admission']['status'],'not_admitted')
        self.assertGreater(report['policies'][0]['deadline_misses'],0)
        self.assertEqual(report['status'],'model_only')

    def test_dbf_is_per_cpu_finite_diagnostic_and_cannot_admit_constrained_tasks(self):
        payload=self.payload();payload['tasks'][0]['deadline_ns']=5
        failed,_,output=self.invoke(json.dumps(payload).encode())
        self.assertEqual(failed.returncode,2);self.assertFalse(output.exists())
        payload['dbf_horizon_ns']=20
        result,_,out=self.invoke(json.dumps(payload).encode())
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(out.read_text())
        self.assertFalse(report['reservation_admission']['accepted'])
        self.assertEqual([x['cpu'] for x in report['dbf_by_cpu']],[1,3])
        self.assertTrue(all(x['scope']=='finite_diagnostic_only' for x in report['dbf_by_cpu']))

    def test_global_limits_reject_without_partial_output(self):
        payload=self.payload()
        for task in payload['tasks']:
            task.update(runtime_ns=1,deadline_ns=1,period_ns=1)
        payload['horizon_ns']=2049
        result,_,out=self.invoke(json.dumps(payload).encode())
        self.assertEqual(result.returncode,2);self.assertIn('global job limit',result.stderr)
        self.assertFalse(out.exists())
        payload['horizon_ns']=2;payload['dbf_horizon_ns']=6000
        result,_,out=self.invoke(json.dumps(payload).encode())
        self.assertEqual(result.returncode,2);self.assertIn('global DBF enumeration',result.stderr)
        self.assertFalse(out.exists())

    def test_invalid_policy_arguments_reject_without_report(self):
        for args in [('--policies','edf,edf'),('--policies','unknown'),
                     ('--policies','rr'),('--policies','rr','--quantum-ns','0'),
                     ('--policies','edf','--quantum-ns','1')]:
            with self.subTest(args=args):
                result,_,out=self.invoke(json.dumps(self.payload()).encode(),*args)
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertFalse(out.exists())

    def test_hash_is_of_parsed_bytes_and_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.json';output=Path(tmp)/'report.json'
            raw=json.dumps(self.payload()).encode();source.write_bytes(raw)
            original=partitioned._json
            def mutate(path):
                parsed=original(path);path.write_text('{}');return parsed
            with patch.object(partitioned,'_json',mutate):
                self.assertEqual(partitioned.main(['--input',str(source),'--output',str(output)]),0)
            self.assertEqual(json.loads(output.read_text())['input_sha256'],hashlib.sha256(raw).hexdigest())
            before=output.read_bytes()
            self.assertEqual(partitioned.main(['--input',str(source),'--output',str(output)]),2)
            self.assertEqual(output.read_bytes(),before)

    def test_output_competitor_and_dangling_symlink_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.json';output=Path(tmp)/'report.json';target=Path(tmp)/'target'
            source.write_text(json.dumps(self.payload()));output.symlink_to(target)
            self.assertEqual(partitioned.main(['--input',str(source),'--output',str(output)]),2)
            self.assertTrue(output.is_symlink());self.assertFalse(target.exists());output.unlink()
            original=partitioned.build_report
            def compete(*args,**kwargs):
                result=original(*args,**kwargs);output.write_text('other writer');return result
            with patch.object(partitioned,'build_report',compete):
                self.assertEqual(partitioned.main(['--input',str(source),'--output',str(output)]),2)
            self.assertEqual(output.read_text(),'other writer')

    def test_special_and_large_inputs_fail_promptly(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.json';output=Path(tmp)/'report.json'
            os.mkfifo(source)
            result=subprocess.run([sys.executable,str(CLI),'--input',str(source),'--output',str(output)],
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,2);self.assertFalse(output.exists())
            source.unlink();source.write_bytes(b' '*(1048576+1))
            result=subprocess.run([sys.executable,str(CLI),'--input',str(source),'--output',str(output)],
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,2);self.assertFalse(output.exists())

    def test_unencodable_report_is_rejected_before_output_creation(self):
        payload=self.payload()
        payload['tasks'][0]['name']='bad-\ud800-name'
        result,_,out=self.invoke(json.dumps(payload).encode())
        self.assertEqual(result.returncode,2)
        self.assertIn('surrogates not allowed',result.stderr)
        self.assertFalse(os.path.lexists(out))

    def test_two_cpu_report_contains_independent_timelines_and_exact_totals(self):
        self.assertTrue(CLI.is_file(), 'partitioned CLI is missing')
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.json'; output=Path(tmp)/'report.json'
            raw=(json.dumps(self.payload())+'\n').encode(); source.write_bytes(raw)
            result=subprocess.run([sys.executable,str(CLI),'--input',str(source),'--output',str(output),
                                   '--policies','edf,fifo,rr,reservation_edf','--quantum-ns','1'],
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(output.read_text())
            self.assertEqual(report['status'],'model_only')
            self.assertEqual(report['schema'],'xtos-scheduler-partitioned-report-v1')
            self.assertEqual(report['partition_cpus'],[1,3,7])
            self.assertEqual(report['unused_cpus'],[7])
            self.assertEqual(report['input_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertTrue(report['reservation_admission']['accepted'])
            self.assertEqual(len(report['policies']),4)
            for policy in report['policies']:
                self.assertEqual(policy['job_count'],4)
                self.assertEqual(policy['deadline_misses'],0)
                self.assertEqual(policy['budget_exhaustions'],0)
                self.assertEqual([cpu['cpu'] for cpu in policy['cpus']],[1,3])
                self.assertEqual([cpu['jobs'][0]['start_ns'] for cpu in policy['cpus']],[0,0])
                self.assertEqual([cpu['job_count'] for cpu in policy['cpus']],[2,2])
            self.assertEqual(source.read_bytes(),raw)


if __name__=='__main__':
    unittest.main()

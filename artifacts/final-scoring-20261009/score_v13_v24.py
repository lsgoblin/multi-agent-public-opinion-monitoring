import hashlib
import json
from pathlib import Path

from riskshield.day5_evaluation import INPUT_SCHEMA, LABEL_SCHEMA, evaluate_documents

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / 'v13-v24-evaluator-final-v2.json'


def read(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding='utf-8'))


def digest(rel: str) -> str:
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()


def main() -> None:
    input_rel = 'data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json'
    prediction_rel = 'artifacts/final-integration-review/v13-pilot/predictions-v13.json'
    source = read(input_rel)
    event = source['events'][0]
    prediction = read(prediction_rel)['predictions'][0]
    case_id = event['case_id']
    event_id = event['event_id']
    evaluation_id = f"{case_id}__direction__e-handoff-v13"

    sources = []
    for index, source_row in enumerate(event['cutoff_sources']):
        pointer = f'/events/0/cutoff_sources/{index}'
        sources.append({
            'record_id': source_row['record_id'],
            'publisher': source_row.get('publisher'),
            'source_version': source_row['source_version'],
            'available_at': source_row['available_at'],
            'source_record_ref': {'path': input_rel, 'pointer': pointer + '/record_id'},
            'source_version_ref': {'path': input_rel, 'pointer': pointer + '/source_version'},
            'source_available_at_ref': {'path': input_rel, 'pointer': pointer + '/available_at'},
            'visibility_evidence_ref': {'path': input_rel, 'pointer': pointer + '/available_at'},
        })

    inputs = {
        'schema': INPUT_SCHEMA,
        'dataset_version': source['dataset_version'] + '__' + prediction['execution_mode'],
        'cases': [{
            'evaluation_id': evaluation_id,
            'case_id': case_id,
            'event_id': event_id,
            'unit_id': 'event_cutoff_summary',
            'unit_kind': 'event_summary',
            'split': event['split'],
            'data_mode': 'real_historical',
            'target_record_id': event['cutoff_sources'][0]['record_id'],
            'source_version': source['dataset_version'],
            'cutoff': event['cutoff'],
            'cutoff_evidence_ref': {'path': input_rel, 'pointer': '/events/0/cutoff'},
            'sources': sources,
            'predictions': {
                'direction': {
                    'label': prediction['direction'],
                    'execution_mode': prediction['execution_mode'],
                    'result_evidence_refs': [{'path': prediction_rel, 'pointer': '/predictions/0/direction'}],
                },
                'sentiment': None,
            },
        }],
    }
    labels = {
        'schema': LABEL_SCHEMA,
        'label_version': 'v24_human_labels_only',
        'events': [{'event_id': event_id, 'observed_direction': None, 'evidence_refs': []}],
        'sentiment_labels': [],
    }
    annotation_doc = read('artifacts/final-scoring-20261009/ai-annotations-final-v1.json')
    ai_reference = read('artifacts/final-scoring-20261009/ai-reference-for-scorer.json')
    metrics = evaluate_documents(
        inputs,
        labels,
        input_sha256=digest(input_rel),
        labels_sha256=digest('data/evaluation/final-benchmark/manifests/final-reconciliation-v24.json'),
        evidence_root=ROOT,
        ai_reference={'reference_model': annotation_doc['annotator_model'], 'annotations': ai_reference['annotations']},
    )
    report = {
        'schema': 'riskshield.day5.evaluation-report.v1',
        'scoring_basis': 'Frozen v24 protocol applied to the latest sealed v13 E result via the existing Day 5 metric engine. Candidate split and offline constant baseline are excluded from formal model claims.',
        'sealed_prediction': {
            'seal_path': 'artifacts/final-integration-review/v13-pilot/seal-v13.json',
            'execution_mode': prediction['execution_mode'],
            'prediction_sha256': digest(prediction_rel),
            'input_sha256': digest(input_rel),
        },
        'metrics': metrics,
        'input_case': inputs['cases'][0],
        'ai_reference_source_sha256': digest('artifacts/final-scoring-20261009/ai-reference-for-scorer.json'),
        'formal_human_labels_added': 0,
    }
    with OUT.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')
    print(json.dumps({
        'report': str(OUT),
        'direction': metrics['direction_agreement'],
        'sentiment': metrics['sentiment_three_class'],
        'ai_reference': metrics['ai_reference_agreement'],
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()

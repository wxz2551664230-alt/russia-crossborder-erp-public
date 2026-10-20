import importlib.util
from pathlib import Path


def test_public_history_strips_old_pc_paths_and_secrets():
    path = Path(__file__).resolve().parents[1] / 'scripts/sync_catalog_ozon_publications.py'
    spec = importlib.util.spec_from_file_location('sync_catalog_ozon_publications', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.public({
        'source_snapshot': {'workbook': r'D:\Desktop\old\source.xlsx'},
        'ai_preparation': {'generated_image_path': 'C:/old/image.jpg'},
        'traceback': r'File D:\Desktop\old\worker.py',
        'secrets_encrypted': 'private',
        'images': ['https://oss.example/image.jpg'],
    })
    assert result == {
        'source_snapshot': {'workbook': ''},
        'ai_preparation': {'generated_image_path': ''},
        'traceback': '',
        'images': ['https://oss.example/image.jpg'],
    }

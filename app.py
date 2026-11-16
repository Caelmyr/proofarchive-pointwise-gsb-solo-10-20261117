import os
from image_lab import create_app
from proofarchive import install
from waitress import serve
root = os.environ.get('PROOFARCHIVE_DATA_DIR', 'data')
app = create_app(os.path.join(root, 'image-lab'))
install(app, os.path.join(root, 'archive'))
if __name__ == '__main__':
    serve(app, host=os.environ.get('HOST', '0.0.0.0'), port=int(os.environ.get('PORT', '8913')), threads=4)

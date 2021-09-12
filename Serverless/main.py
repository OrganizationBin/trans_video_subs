
import os
from videosub import cloudrun_entry
from flask import Flask, request


app = Flask(__name__)
# [END eventarc_audit_storage_server]

@app.route("/", methods=["POST"])
def index():
    object = request.headers.get('ce-subject').split("/")[1]
    bucket = request.headers.get('ce-bucket')
    print(f"New video: {bucket, object}")
    cloudrun_entry(bucket, object)
    return ("Finish video caption", 204)


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
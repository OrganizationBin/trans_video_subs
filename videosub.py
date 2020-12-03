from google.cloud import storage
from ffmpy import FFmpeg, FFprobe
import json
import os
from concurrent import futures
from subprocess import PIPE
from speech2txt import speech2txt
from translate import batch_translate_text
from txt2srt import txt2srt
import argparse

support_format = [".mov", ".mp4", ".mkv", ".avi"]
storage_client = storage.Client()

parser = argparse.ArgumentParser()
parser.add_argument("--bucket", type=str, default="my-video-en")
parser.add_argument("--video_src_language", type=str, default="en-US")
# Speech to text language code: https://cloud.google.com/speech-to-text/docs/languages
parser.add_argument("--translate_src_language", type=str, default="en")
parser.add_argument("--translate_des_language", type=str, default="zh")
# Translate language code: https://cloud.google.com/translate/docs/languages
parser.add_argument("--translate_location", type=str, default="us-central1")
parser.add_argument("--merge_sub_to_video", type=bool, default=True)
parser.add_argument("--parallel_threads", type=int, default=1)
args = parser.parse_args()

bucket_in = args.bucket
bucket_tmp = bucket_in + "-tmp"
bucket_out = bucket_in + "-out"
video_src_language_code = args.video_src_language
translate_src_code = args.translate_src_language
translate_des_code = args.translate_des_language
project_id = storage_client.project
translate_location = args.translate_location  # Traslate API running region
merge_sub_to_video = args.merge_sub_to_video  # Merge subtitle into video (Hard merge)
parallel_threads = args.parallel_threads  # Concurrent processing threads


def audio_to_file(filename, filename_audio):
    if not os.path.exists(filename_audio):
        ff = FFmpeg(inputs={filename: None},
                    outputs={filename_audio: '-vn -y -loglevel warning'}
                    )
        print(ff.cmd)
        ff.run()


def get_audio_info(filename_audio):
    fr = FFprobe(global_options='-of json -show_streams -select_streams a',
                 inputs={filename_audio: None},
                 )
    print(fr.cmd)
    res = fr.run(stdout=PIPE, stderr=PIPE)
    stream_detail = json.loads(res[0]).get('streams')[0]
    sample_rate = int(stream_detail['sample_rate'])
    channels = int(stream_detail['channels'])
    print(filename_audio, 'sample_rate:', sample_rate, 'channels: ', channels)
    return sample_rate, channels


def upload(bucket, localfile, bucketfile):
    bucket = storage_client.bucket(bucket)
    blob = bucket.blob(bucketfile)
    blob.upload_from_filename(localfile)


def download(bucket, localfile, bucketfile):
    bucket = storage_client.bucket(bucket)
    blob = bucket.blob(bucketfile)
    blob.download_to_filename(localfile)
    # TODO: Now not support sub folder


def process_video(filename):
    print("Start processing...", filename)
    out_file = os.path.splitext(filename)[0]  # Pre-fix of the file

    # Download video from gs://in
    download(bucket_in, filename, filename)

    # Get audio track to file
    filename_audio = out_file + ".flac"
    audio_to_file(filename, filename_audio)

    # Get audio detail info
    sample_rate, channels = get_audio_info(filename_audio)

    # Upload audio to gs://tmp
    upload(bucket=bucket_tmp,
           localfile=filename_audio,
           bucketfile=f"{out_file}/{filename_audio}")

    # Speech to text
    storage_uri = f"gs://{bucket_tmp}/{out_file}/{filename_audio}"
    speech2txt(
        sample_rate=sample_rate,
        channels=channels,
        language_code=video_src_language_code,
        storage_uri=storage_uri,
        out_file=out_file
    )

    # Upload txt file to bucket_tmp
    input_uri = f"gs://{bucket_tmp}/{out_file}/{out_file}.{video_src_language_code}.txt"
    upload(bucket=bucket_tmp,
           localfile=f"{out_file}.{video_src_language_code}.txt",
           bucketfile=f"{out_file}/{out_file}.{video_src_language_code}.txt")
    upload(bucket=bucket_tmp,
           localfile=f"{out_file}.{video_src_language_code}.srt",
           bucketfile=f"{out_file}/{out_file}.{video_src_language_code}.srt")

    # Submit translate
    output_uri_prefix = f"gs://{bucket_tmp}/{out_file}-translated/"

    clean_bucket(bucket_tmp, out_file + "-translated/")  # If output not empty, then clean them

    batch_translate_text(
        input_uri, output_uri_prefix, project_id, translate_location, translate_src_code, translate_des_code
    )

    # get translate txt and compose into srt
    translated_txt = f"{out_file}-translated/{bucket_tmp}_{out_file}_{out_file}.{video_src_language_code}_{translate_des_code}_translations.txt"
    download(bucket=bucket_tmp,
             localfile=f"{out_file}.{translate_des_code}.txt",
             bucketfile=translated_txt)

    txt2srt(
        orgfile=f"{out_file}.{video_src_language_code}.srt",
        langfile=f"{out_file}.{translate_des_code}.txt",
        lang=translate_des_code,
        out_file=out_file
    )

    # upload srt to gs://output
    out_srt = f"{out_file}.{translate_des_code}.srt"
    upload(bucket_out, out_srt, out_srt)

    if merge_sub_to_video:
        out_video = f"{out_file}.{translate_des_code}{os.path.splitext(filename)[1]}"
        # ffmpeg convert video to video with hard-subtitles
        ff = FFmpeg(inputs={filename: None},
                    outputs={out_video: f"-y -vf 'subtitles={out_file}.{translate_des_code}.srt' -loglevel warning"}
                    )
        print(ff.cmd)
        ff.run()

        # Upload video to gs://output
        upload(bucket_out, out_video, out_video)

    # Delete all local temp files
    clean_local(out_file)


def create_bucket(buckets):
    for b in buckets:
        bucket = storage_client.bucket(b)
        if not bucket.exists():
            storage_client.create_bucket(bucket)


def clean_bucket(bucket, prefix):
    file_list = storage_client.list_blobs(bucket, prefix=prefix)
    b = storage_client.bucket(bucket)
    for file in file_list:
        print(f"Output folder not empty, clean... gs://{bucket}/{file.name}")
        b.blob(file.name).delete()


def clean_local(out_file):
    f_list = os.listdir(os.getcwd())
    for f in f_list:
        if f.startswith(out_file):
            os.remove(f)


def main():
    # Create tmp and output bucket
    create_bucket([bucket_tmp, bucket_out])

    # list gs and Download video from gs to local
    file_list = storage_client.list_blobs(bucket_in)

    # Pallaral process
    with futures.ThreadPoolExecutor(max_workers=parallel_threads) as pool:
        for blob in file_list:
            filename = blob.name
            if os.path.splitext(filename)[1] in support_format:
                pool.submit(process_video, filename)
            else:
                print("Not support format, skip...", filename)

    print(f"/n Finished all subtitiles and videos output to gs://{bucket_out}")


if __name__ == '__main__':
    main()

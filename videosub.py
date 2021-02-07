from google.cloud import storage
from ffmpy import FFmpeg, FFprobe
import json
import os
import re
from concurrent import futures
from subprocess import PIPE
from speech2txt import speech2txt
from translate import batch_translate_text
from txt2srt import txt2srt
import argparse

support_format = [".mov", ".mp4", ".mkv", ".avi", ".webm"]
storage_client = storage.Client()

parser = argparse.ArgumentParser()
parser.add_argument("--bucket", type=str, default="hzb-video-en")

parser.add_argument("--video_src_language", type=str, default="en-US")
# Speech to text language code: https://cloud.google.com/speech-to-text/docs/languages

parser.add_argument("--translate_src_language", type=str, default="en")
parser.add_argument("--translate_des_language", type=str, default="zh")
# Translate language code: https://cloud.google.com/translate/docs/languages

parser.add_argument("--translate_location", type=str, default="us-central1")
# Because we use batch translate, only support us-central1

parser.add_argument("--merge_sub_to_video", type=str, default="True")
# Hard-encode the srt subtitle file into video

parser.add_argument("--parallel_threads", type=int, default=1)
# Processing videos in parallel

parser.add_argument("--local_file", type=str, default="NONE")
# If set local_file (only one filename in the same path as this code), it will not list the bucket of the source
# You still need to set a fake bucket name with --bucket para, it is for creating tmp and output bucket

args = parser.parse_args()
bucket_in = args.bucket
bucket_tmp = bucket_in + "-tmp"
bucket_out = bucket_in + "-out"
video_src_language_code = args.video_src_language
translate_src_code = args.translate_src_language
translate_des_code = args.translate_des_language
project_id = storage_client.project
translate_location = args.translate_location  # Traslate API running region
merge_sub_to_video = args.merge_sub_to_video.lower() == "true"  # Merge subtitle into video (Hard merge)
parallel_threads = args.parallel_threads  # Concurrent processing threads
local_file = args.local_file


def audio_to_file(filename, filename_audio):
    try:
        if not os.path.exists(filename_audio):
            ff = FFmpeg(inputs={filename: None},
                        outputs={filename_audio: '-vn -y -loglevel warning'}
                        )
            print(ff.cmd)
            ff.run()
    except Exception as e:
        print(f"ERROR while audio_to_file {filename}: ", e)


def get_audio_info(filename_audio):
    try:
        fr = FFprobe(global_options='-of json -show_streams -select_streams a',
                     inputs={filename_audio: None},
                     )
        print(fr.cmd)
        res = fr.run(stdout=PIPE, stderr=PIPE)
        stream_detail = json.loads(res[0]).get('streams')[0]
        sample_rate = int(stream_detail['sample_rate'])
        channels = int(stream_detail['channels'])
        print(filename_audio, 'sample_rate:', sample_rate, 'channels: ', channels)
    except Exception as e:
        print(f"ERROR while get_audio_info {filename_audio}: ", e)
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
    print("! Start processing...", filename)
    out_file = os.path.splitext(filename)[0]  # Pre-fix of the file

    # Download video from gs://in
    if local_file == "NONE":
        download(bucket_in, filename, filename)

    # Get audio track to file
    filename_audio = out_file + ".flac"
    audio_to_file(filename, filename_audio)

    # Get audio detail info
    sample_rate, channels = get_audio_info(filename_audio)

    # Upload audio to gs://tmp
    # upload(bucket=bucket_tmp,
    #        localfile=filename_audio,
    #        bucketfile=f"{out_file}/{filename_audio}")

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
        try:
            out_video = f"{out_file}.{translate_des_code}{os.path.splitext(filename)[1]}"
            # ffmpeg convert video to video with hard-subtitles
            ff = FFmpeg(inputs={filename: None},
                        outputs={out_video: f"-y -vf 'subtitles={out_file}.{translate_des_code}.srt'"}
                        )
            print(ff.cmd)
            ff.run()

            # Upload video to gs://output
            upload(bucket_out, out_video, out_video)
            print(f"Uploaded video with sub to {out_video}")
        except Exception as e:
            print(f"ERROR while merge_sub_to_video {out_srt}: ", e)

    # Delete all local temp files
    if local_file == "NONE":
        clean_local(out_file)
    return


def create_bucket(buckets, bucket_in):
    # Create bucket in the same location as bucket_in
    r = storage_client.get_bucket(bucket_in)
    location = r.location

    for b in buckets:
        bucket = storage_client.bucket(b)
        if not bucket.exists():
            storage_client.create_bucket(bucket, location=location)


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


def make_list(file_inter):
    m_list = []
    for f in file_inter:
        m_list.append(f.name)
    return m_list


def compare_bucket(bucket_in, bucket_out, lang):
    print(f"Comparing input and output bucket")
    source_inter = storage_client.list_blobs(bucket_in)
    target_inter = storage_client.list_blobs(bucket_out)

    src_bucket = make_list(source_inter)
    des_bucket = make_list(target_inter)

    delta_list = []
    for s in src_bucket:
        prefix = os.path.splitext(s)[0]
        surfix = os.path.splitext(s)[1]
        full = prefix + "." + lang + surfix
        if full not in des_bucket:
            delta_list.append(s)
    if len(delta_list) != 0:
        print("There files are not finished output. Please check:", delta_list)
    else:
        print("Compare result: Match! All files output!")
    return


def bucket_file_name(bucket):
    list = storage_client.list_blobs(bucket)
    b = storage_client.bucket(bucket)

    file_list = []
    rstr = r"[\/\\\:\*\?\"\<\>\|\[\]\'\ \@]"  # '/ \ : * ? " < > | [ ] ' @ '
    for f in list:
        filename = f.name
        # Change filename if match special character
        if re.search(rstr, filename):
            filename_old = filename
            filename = re.sub(rstr, "_", filename)
            blob = b.blob(filename_old)
            b.rename_blob(blob, filename)

        file_list.append(filename)

    return file_list


def main():
    # Create tmp and output bucket
    create_bucket([bucket_tmp, bucket_out], bucket_in)

    # List files on bucket and change special character
    if local_file == "NONE":
        file_list = bucket_file_name(bucket_in)
    else:
        file_list = [local_file]
    # Pallaral process
    with futures.ThreadPoolExecutor(max_workers=parallel_threads) as pool:
        for filename in file_list:
            if os.path.splitext(filename)[1] in support_format:
                pool.submit(process_video, filename)
            else:
                print("Not support format, skip...", filename)

    print(f"! Finished all subtitiles and videos output to gs://{bucket_out}")

    # Compare source bucket and output bucket
    compare_bucket(bucket_in, bucket_out, translate_des_code)


if __name__ == '__main__':
    main()

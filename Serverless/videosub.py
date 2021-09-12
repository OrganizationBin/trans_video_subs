from google.cloud import storage
from ffmpy import FFmpeg, FFprobe
import json
import os
import re
import sys
import platform
from concurrent import futures
from subprocess import PIPE

from speech2txt import speech2txt
from translate import batch_translate_text
from txt2srt import txt2srt
import argparse

support_format = [".mov", ".mp4", ".mkv", ".avi", ".webm", ".flac"]
storage_client = storage.Client()
project_id = storage_client.project
translate_location = "us-central1"
global bucket_org, bucket_in, bucket_tmp, bucket_out, video_src_language_code, translate_src_code, translate_des_code, merge_sub_to_video, two_step_convert, parallel_threads, local_file

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
                     inputs={filename_audio: None}
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
    if two_step_convert.lower() != "second":
        if local_file == "NONE":
            print("Download video from:", bucket_in, filename)
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
        s2t_re = speech2txt(
            sample_rate=sample_rate,
            channels=channels,
            language_code=video_src_language_code,
            storage_uri=storage_uri,
            out_file=out_file
        )
        if s2t_re == "ERR":
            return "ERR"

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

        translate_re = batch_translate_text(
            input_uri, output_uri_prefix, project_id, translate_location, translate_src_code, translate_des_code
        )
        if translate_re == "ERR":
            return "ERR"

        # get translate txt and compose into srt
        translated_txt = f"{out_file}-translated/{bucket_tmp}_{out_file}_{out_file}.{video_src_language_code}_{translate_des_code}_translations.txt"
        print("get translate txt and compose into srt: ", translated_txt)
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

    if merge_sub_to_video and two_step_convert.lower() != "first":
        try:
            out_video = f"{out_file}.{translate_des_code}{os.path.splitext(filename)[1]}"
            # ffmpeg convert video to video with hard-subtitles
            ff = FFmpeg(inputs={filename: None},
                        outputs={out_video: f"-y -vf 'subtitles={out_file}.{translate_des_code}.srt':force_style='Fontsize=24'"}
                        )
            print(ff.cmd)
            ff.run()

            # Upload video to gs://output
            upload(bucket_out, out_video, out_video)
            print(f"Uploaded video with sub to {out_video}")
        except Exception as e:
            print(f"ERROR while merge_sub_to_video {out_srt}: ", e)

    # Delete all local temp files
    if two_step_convert.lower() != "first" and local_file == "NONE":
        clean_local(out_file)
    return


def create_bucket(buckets, bucket_org):
    # Create bucket in the same location as bucket
    r = storage_client.get_bucket(bucket_org)
    location = r.location

    for b in buckets:
        bb = storage_client.bucket(b)
        if not bb.exists():
            storage_client.create_bucket(bb, location=location)


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
        full = prefix + "." + lang + ".srt"
        if full not in des_bucket:
            delta_list.append(s)
    if len(delta_list) != 0:
        print("There files are not finished output. Please check:", delta_list)
    else:
        print("Compare result: Match! All files output!")
    return


def bucket_file_name(bucket_org):
    list = storage_client.list_blobs(bucket_org)
    file_list = []
    rstr = r"[\/\\\:\*\?\"\<\>\|\[\]\'\ \@\’\,]"  # '/ \ : * ? " < > | [ ] ' @ '
    for f in list:
        filename = f.name
        # Change filename if match special character
        filename_old = filename
        if re.search(rstr, filename):
            filename = re.sub(rstr, "_", filename)
        source_bucket = storage_client.bucket(bucket_org)
        source_blob = source_bucket.blob(filename_old)
        destination_bucket = storage_client.bucket(bucket_in)
        source_bucket.copy_blob(
            source_blob, destination_bucket, filename
        )

        file_list.append(filename)
    return file_list


def main():
    global bucket_org, bucket_in, bucket_tmp, bucket_out, video_src_language_code, translate_src_code, translate_des_code, translate_location, merge_sub_to_video, two_step_convert, parallel_threads, local_file
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

    parser.add_argument("--parallel_threads", type=int, default=4)
    # Processing videos in parallel

    parser.add_argument("--local_file", type=str, default="NONE")
    # If set local_file (only one filename in the same path as this code), it will not list the bucket of the source
    # You still need to set a fake bucket name with --bucket para, it is for creating tmp and output bucket

    parser.add_argument("--two_step_convert", type=str, default="False")
    # Two steps Hard-encode the srt subtitle file into video, 
    # "First" is output srt and don't delete the video
    # "Second" is hard-encode video and clean

    parser.add_argument("--gui", action='store_true')
    # Enable GUI

    args = parser.parse_args()
    bucket_org = args.bucket
    bucket_in = bucket_org + "-in"
    bucket_tmp = bucket_org + "-tmp"
    bucket_out = bucket_org + "-out"
    video_src_language_code = args.video_src_language
    translate_src_code = args.translate_src_language
    translate_des_code = args.translate_des_language
    translate_location = args.translate_location  # Traslate API running region
    merge_sub_to_video = args.merge_sub_to_video.lower() == "true"  # Merge subtitle into video (Hard merge)
    parallel_threads = args.parallel_threads  # Concurrent processing threads
    local_file = args.local_file
    two_step_convert = args.two_step_convert

    # Set GUI
    if platform.uname()[0] == 'Windows':
        gui = True
    elif args.gui:
        gui = True
    else:
        gui = False 
    if gui:
        from tkinter import Tk, filedialog, END, StringVar, BooleanVar, messagebox
        from tkinter.ttk import Combobox, Label, Button, Entry, Spinbox, Checkbutton

        def ListBuckets():
            buckets = storage_client.list_buckets()
            Bucket_txt['values'] = [b.name for b in buckets]
            Bucket_txt.current(0)
        
        def ListObjects(bucket):
            objects = storage_client.list_blobs(bucket)
            objects_list = [o.name for o in objects]
            return len(objects_list)

        window = Tk()
        window.title("Translate video with caption")
        window.geometry('650x160')
        window.configure(background='#ECECEC')
        window.protocol("WM_DELETE_WINDOW", sys.exit)

        Label(window, text='Bucket Name').grid(column=0, row=0, sticky='w', padx=2, pady=2)
        Bucket_txt = Combobox(window, width=35)
        Bucket_txt.grid(column=1, row=0, sticky='w', padx=2, pady=2)
        Button(window, text="List Buckets", width=10, command=ListBuckets) \
            .grid(column=2, row=0, sticky='w', padx=2, pady=2)
        
        Label(window, text="Merge Caption to Video").grid(column=0, row=1, sticky='w', padx=2, pady=2)
        merge_sub_to_video_txt = Combobox(window, width=15, state="readonly")
        merge_sub_to_video_txt['values'] = ["True", "False"]
        merge_sub_to_video_txt.grid(column=1, row=1, sticky='w', padx=2, pady=2)
        merge_sub_to_video_txt.current(0)

        Label(window, text="Two Step Merging").grid(column=0, row=2, sticky='w', padx=2, pady=2)
        two_step_convert_txt = Combobox(window, width=15, state="readonly")
        two_step_convert_txt['values'] = ["False", "First", "Second"]
        two_step_convert_txt.grid(column=1, row=2, sticky='w', padx=2, pady=2)
        two_step_convert_txt.current(0)

        Label(window, text="Parallel Threads").grid(column=0, row=3, sticky='w', padx=2, pady=2)
        if parallel_threads < 1 or parallel_threads > 100:
            parallel_threads = 8
        var_f = StringVar()
        var_f.set(str(parallel_threads))
        parallel_threads_txt = Spinbox(window, from_=1, to=100, width=15, textvariable=var_f)
        parallel_threads_txt.grid(column=1, row=3, sticky='w', padx=2, pady=2)

        def close():
            window.withdraw()
            bucket_org = Bucket_txt.get()
            if bucket_org == "":
                messagebox.showinfo("INFO","Please select or input bucket name!")
                window.deiconify()
                return
            list_len = ListObjects(bucket_org)
            ok = messagebox.askokcancel("START", f"Start to translate {list_len} videos")
            if not ok:
                window.deiconify()
                return
            window.quit()
        Button(window, text="Start", width=15, command=close).grid(column=1, row=4, padx=5, pady=5)
        window.mainloop()

        bucket_org = Bucket_txt.get()
        bucket_in = bucket_org + "-in"
        bucket_tmp = bucket_org + "-tmp"
        bucket_out = bucket_org + "-out"
        merge_sub_to_video = merge_sub_to_video_txt.get().lower() == "true"
        two_step_convert = two_step_convert_txt.get()
        parallel_threads = int(parallel_threads_txt.get())

    # Create tmp and output bucket
    create_bucket([bucket_tmp, bucket_out, bucket_in], bucket_org)

    # List files on bucket and change special character
    if local_file == "NONE":
        file_list = bucket_file_name(bucket_org)
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
    return

def cloudrun_entry(bucket, filename):
    """
    While deploy to CloudRun, there should be Environment variables:
    video_src_language_code: en-US
    translate_src_code: en
    translate_des_code: zh
    merge_sub_to_video: False
    two_step_convert: False
    """
    if os.path.splitext(filename)[1] not in support_format:
        print("Not support format:", filename)
        return

    global bucket_org, bucket_in, bucket_tmp, bucket_out, video_src_language_code, translate_src_code, translate_des_code, translate_location, merge_sub_to_video, two_step_convert, parallel_threads, local_file
    
    bucket_org = bucket
    bucket_in = bucket_org + "-in"
    bucket_tmp = bucket_org + "-tmp"
    bucket_out = bucket_org + "-out"
    create_bucket([bucket_tmp, bucket_out, bucket_in], bucket)
    video_src_language_code = os.environ.get("video_src_language_code")
    translate_src_code = os.environ.get("translate_src_code")
    translate_des_code = os.environ.get("translate_des_code")
    merge_sub_to_video = os.environ.get("merge_sub_to_video").lower == "true"
    two_step_convert = os.environ.get("two_step_convert")
    parallel_threads = 1
    local_file = "NONE"
    
    rstr = r"[\/\\\:\*\?\"\<\>\|\[\]\'\ \@\’\,]"  # '/ \ : * ? " < > | [ ] ' @ '
    filename_old = filename
    if re.search(rstr, filename):
        filename = re.sub(rstr, "_", filename)
    source_bucket = storage_client.bucket(bucket_org)
    source_blob = source_bucket.blob(filename_old)
    destination_bucket = storage_client.bucket(bucket_in)
    source_bucket.copy_blob(
        source_blob, destination_bucket, filename
    )
    process_video(filename)

if __name__ == '__main__':

    main()

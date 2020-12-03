import srt
import time
from google.cloud import speech


def long_running_recognize(sample_rate, channels, language_code, storage_uri):
    """
    Transcribe long audio file from Cloud Storage using asynchronous speech
    recognition

    Args:
      storage_uri URI for audio file in GCS, e.g. gs://[BUCKET]/[FILE]
      sample_rate of the audio, e.g. 44100
      channels of audio, e.g. 2
      language_code of audio, e.g. en-US
        # Speech to text language code: https://cloud.google.com/speech-to-text/docs/languages
    """

    print("Transcribing {} ...".format(storage_uri))
    client = speech.SpeechClient()

    # Encoding of audio data sent, recommend use loseless codec. Here should match ffmpeg output codec
    encoding = speech.RecognitionConfig.AudioEncoding.FLAC
    # Supported encoding: https://cloud.google.com/speech-to-text/docs/encoding#audio-encodings

    config = {
        "enable_word_time_offsets": True,
        "enable_automatic_punctuation": True,
        "sample_rate_hertz": sample_rate,
        "language_code": language_code,
        "encoding": encoding,
        "audio_channel_count": channels,
        "model": "video"
    }
    audio = {"uri": storage_uri}

    operation = client.long_running_recognize(
        request={
            "config": config,
            "audio": audio,
        }
    )
    response = operation.result(timeout=3600)

    subs = []

    for result in response.results:
        # First alternative is the most probable result
        subs = break_sentences(subs, result.alternatives[0])

    return subs


def break_sentences(subs, alternative, max_chars=40):
    firstword = True
    charcount = 0
    idx = len(subs) + 1
    content = ""

    for w in alternative.words:
        if firstword:
            # first word in sentence, record start time
            start_hhmmss = time.strftime('%H:%M:%S', time.gmtime(
                w.start_time.seconds))
            start_ms = int(w.start_time.microseconds / 1000)
            start = start_hhmmss + "," + str(start_ms)

        charcount += len(w.word)
        content += " " + w.word.strip()

        if ("." in w.word or "!" in w.word or "?" in w.word or
                charcount > max_chars or
                ("," in w.word and not firstword)):
            # break sentence at: . ! ? or line length exceeded
            # also break if , and not first word
            end_hhmmss = time.strftime('%H:%M:%S', time.gmtime(
                w.end_time.seconds))
            end_ms = int(w.end_time.microseconds / 1000)
            end = end_hhmmss + "," + str(end_ms)
            subs.append(srt.Subtitle(index=idx,
                                     start=srt.srt_timestamp_to_timedelta(start),
                                     end=srt.srt_timestamp_to_timedelta(end),
                                     content=srt.make_legal_content(content)))
            firstword = True
            idx += 1
            content = ""
            charcount = 0
        else:
            firstword = False
    return subs


def write_srt(out_file, language_code, subs):
    srt_file = f"{out_file}.{language_code}.srt"
    print("Writing subtitles to: {}".format(srt_file))
    with open(srt_file, 'w') as f:
        f.writelines(srt.compose(subs))

    return


def write_txt(out_file, language_code, subs):
    txt_file = f"{out_file}.{language_code}.txt"
    print("Writing text to: {}".format(txt_file))
    with open(txt_file, 'w') as f:
        for s in subs:
            f.write(s.content.strip() + "\n")

    return


def speech2txt(sample_rate, channels, language_code, storage_uri, out_file):
    subs = long_running_recognize(sample_rate, channels, language_code, storage_uri)
    write_srt(out_file, language_code, subs)
    write_txt(out_file, language_code, subs)


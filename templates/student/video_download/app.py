
from flask import *
from pytubefix import YouTube
import os
from datetime import datetime
import shutil

app = Flask(__name__)

i = 0  # rotating index for file naming

# Create required folders
if not os.path.exists('audios'):
    os.mkdir('audios')
if not os.path.exists('videos'):
    os.mkdir('videos')


def clean_url(link: str) -> str:
    """Cleans YouTube URL by removing ?si= and other parameters."""
    return link.split("&")[0].split("?")[0]


def download_audio(link):
    global i

    clean = clean_url(link)

    if len(os.listdir('audios')) > 5:
        shutil.rmtree('audios')
        os.mkdir('audios')

    yt = YouTube(clean)

    audio_stream = yt.streams.filter(only_audio=True).first()
    out_file = audio_stream.download(output_path="audios", filename=f"{i}.mp3")

    os.system(
        f'ffmpeg -y -loop 1 -r 1 -i static/images/flyer.jpg -i audios/{i}.mp3 '
        f'-c:a copy -shortest -c:v libx264 audios/{i}.mp4'
    )

    os.remove(out_file)

    ret_file = f"audios/{i}.mp4"

    i = (i + 1) % 5

    with open("history.txt", "a") as myfile:
        myfile.write(f"\n{datetime.now().strftime('%d/%m/%y__%H:%M:%S')} --> {link}\n")

    return ret_file


def download_video(link):
    global i

    clean = clean_url(link)

    if len(os.listdir('videos')) > 2:
        shutil.rmtree('videos')
        os.mkdir('videos')

    yt = YouTube(clean)

    stream = yt.streams.get_lowest_resolution()
    out_file = stream.download(output_path="videos", filename=f"{i}.mp4")

    ret_file = f"videos/{i}.mp4"

    i = (i + 1) % 2

    with open("history.txt", "a") as myfile:
        myfile.write(f"\n{datetime.now().strftime('%d/%m/%y__%H:%M:%S')} --> {link}\n")

    return ret_file


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/submit_audio', methods=['POST'])
def submit_audio():
    link = request.form.get('link')
    path = download_audio(link)
    return send_file(path, as_attachment=True)


@app.route('/submit', methods=['POST'])
def submit():
    link = request.form.get('link')
    path = download_video(link)
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=False, port=5000, host="0.0.0.0")

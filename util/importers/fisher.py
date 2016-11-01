import fnmatch
import numpy as np
import os
import random
import subprocess
import wave

from glob import glob
from itertools import cycle
from math import ceil
from Queue import PriorityQueue
from Queue import Queue
from threading import Thread
from util.audio import audiofile_to_input_vector
from util.gpu import get_available_gpus
from util.text import texts_to_sparse_tensor, validate_label

class DataSets(object):
    def __init__(self, train, dev, test):
        self._dev = dev
        self._test = test
        self._train = train
    
    @property
    def train(self):
        return self._train
    
    @property
    def dev(self):
        return self._dev
    
    @property
    def test(self):
        return self._test

class DataSet(object):
    def __init__(self, graph, txt_files, thread_count, batch_size, numcep, numcontext):
        self._graph = graph
        self._numcep = numcep
        self._batch_queue = Queue(2 * self._get_device_count())
        self._txt_files = txt_files
        self._batch_size = batch_size
        self._numcontext = numcontext
        self._thread_count = thread_count
        self._files_circular_list = self._create_files_circular_list()
        self._start_queue_threads()
    
    def _get_device_count(self):
        available_gpus = get_available_gpus()
        return max(len(available_gpus), 1)
    
    def _start_queue_threads(self):
        batch_threads = [Thread(target=self._populate_batch_queue) for i in xrange(self._thread_count)]
        for batch_thread in batch_threads:
            batch_thread.daemon = True
            batch_thread.start()
    
    def _create_files_circular_list(self):
        priorityQueue = PriorityQueue()
        for txt_file in self._txt_files:
            wav_file = os.path.splitext(txt_file)[0] + ".wav"
            wav_file_size = os.path.getsize(wav_file)
            priorityQueue.put((wav_file_size, (txt_file, wav_file)))
        files_list = []
        while not priorityQueue.empty():
            priority, (txt_file, wav_file) = priorityQueue.get()
            files_list.append((txt_file, wav_file))
        return cycle(files_list)
    
    def _populate_batch_queue(self):
        with self._graph.as_default():
            n_steps = 0
            sources = []
            targets = []
            batch_index = 0
            for txt_file, wav_file in self._files_circular_list:
                if batch_index == self._batch_size:
                    # Put batch on queue
                    target = texts_to_sparse_tensor(targets)
                    for index, next_source in enumerate(sources):
                        npad = ((0,(n_steps - next_source.shape[0])), (0,0))
                        sources[index] = np.pad(next_source, pad_width=npad, mode='constant')
                    source = np.array(sources)
                    self._batch_queue.put((source, target))
                    n_steps = 0
                    sources = []
                    targets = []
                    batch_index = 0
                next_source = audiofile_to_input_vector(wav_file, self._numcep, self._numcontext)
                if n_steps < next_source.shape[0]:
                    n_steps = next_source.shape[0]
                sources.append(next_source)
                with open(txt_file) as open_txt_file:
                    targets.append(open_txt_file.read())
                batch_index = batch_index + 1
    
    def next_batch(self):
        source, target = self._batch_queue.get()
        return (source, target, source.shape[1])
    
    @property
    def total_batches(self):
        # Note: If len(_txt_files) % _batch_size != 0, this re-uses initial _txt_files
        return int(ceil(float(len(self._txt_files)) /float(self._batch_size)))

def read_data_sets(graph, data_dir, batch_size, numcep, numcontext, thread_count=8):
    # Assume data_dir contains extracted LDC2004S13, LDC2004T19, LDC2005S13, LDC2005T19

    # Conditionally convert Fisher sph data to wav
    _maybe_convert_wav(data_dir, "LDC2004S13", "fisher-2004-wav")
    _maybe_convert_wav(data_dir, "LDC2005S13", "fisher-2005-wav")

    # Conditionally split Fisher wav data
    _maybe_split_wav(data_dir, os.path.join("LDC2004T19", "fe_03_p1_tran", "data", "trans"), "fisher-2004-wav", "fisher-2004-split-wav")
    _maybe_split_wav(data_dir, os.path.join("LDC2005T19", "fe_03_p2_tran", "data", "trans"), "fisher-2005-wav", "fisher-2005-split-wav")

    # Conditionally split Fisher transcriptions
    _maybe_split_transcriptions(data_dir, os.path.join("LDC2004T19", "fe_03_p1_tran", "data", "trans"), "fisher-2004-split-wav")
    _maybe_split_transcriptions(data_dir, os.path.join("LDC2005T19", "fe_03_p2_tran", "data", "trans"), "fisher-2005-split-wav")
    
    # Conditionally split Fisher data into train/validation/test sets
    _maybe_split_sets(data_dir, "fisher-2004-split-wav", "fisher-2004-split-wav-sets")
    _maybe_split_sets(data_dir, "fisher-2005-split-wav", "fisher-2005-split-wav-sets")
    
    # Create train DataSet
    train = _read_data_set(graph, data_dir, "fisher-200?-split-wav-sets/train", thread_count, batch_size, numcep, numcontext)

    # Create dev DataSet
    dev = _read_data_set(graph, data_dir, "fisher-200?-split-wav-sets/dev", thread_count, batch_size, numcep, numcontext)

    # Create test DataSet
    test = _read_data_set(graph, data_dir, "fisher-200?-split-wav-sets/test", thread_count, batch_size, numcep, numcontext)

    # Return DataSets
    return DataSets(train, dev, test)

def _maybe_convert_wav(data_dir, original_data, converted_data):
    source_dir = os.path.join(data_dir, original_data)
    target_dir = os.path.join(data_dir, converted_data)
    
    # Conditionally convert sph files to wav files
    if os.path.exists(target_dir):
        print("skipping maybe_convert_wav")
        return
    
    # Create target_dir
    os.makedirs(target_dir)
    
    # Loop over sph files in source_dir and convert each to 16-bit PCM wav
    for root, dirnames, filenames in os.walk(source_dir):
        for filename in fnmatch.filter(filenames, "*.sph"):
            sph_file = os.path.join(root, filename)
            for channel in ["1", "2"]:
                wav_filename = os.path.splitext(os.path.basename(sph_file))[0] + "_c" + channel + ".wav"
                wav_file = os.path.join(target_dir, wav_filename)
                print("converting {} to {}".format(sph_file, wav_file))
                subprocess.check_call(["sph2pipe", "-c", channel, "-p", "-f", "rif", sph_file, wav_file])

def _parse_transcriptions(trans_file):
    segments = []
    with open(trans_file, "r") as fin:
        for line in fin:
            if line.startswith("#") or len(line) <= 1:
                continue
            
            start_time_beg = 0
            start_time_end = line.find(" ", start_time_beg)
            
            stop_time_beg = start_time_end + 1
            stop_time_end = line.find(" ", stop_time_beg)
            
            speaker_beg = stop_time_end + 1
            speaker_end = line.find(" ", speaker_beg)
            
            transcript_beg = speaker_end + 1
            transcript_end = len(line)
            
            segments.append({
                "start_time": float(line[start_time_beg:start_time_end]),
                "stop_time": float(line[stop_time_beg:stop_time_end]),
                "speaker": line[speaker_beg:speaker_end],
                "transcript": line[transcript_beg:transcript_end].strip(),
            })
    return segments

def _maybe_split_wav(data_dir, trans_data, original_data, converted_data):
    trans_dir = os.path.join(data_dir, trans_data)
    source_dir = os.path.join(data_dir, original_data)
    target_dir = os.path.join(data_dir, converted_data)
    
    if os.path.exists(target_dir):
        print("skipping maybe_split_wav")
        return
    
    os.makedirs(target_dir)
    
    # Loop over transcription files and split corresponding wav
    for root, dirnames, filenames in os.walk(trans_dir):
        for filename in fnmatch.filter(filenames, "*.txt"):
            trans_file = os.path.join(root, filename)
            segments = _parse_transcriptions(trans_file)
            
            # Open wav corresponding to transcription file
            wav_filenames = [os.path.splitext(os.path.basename(trans_file))[0] + "_c" + channel + ".wav" for channel in ["1", "2"]]
            wav_files = [os.path.join(source_dir, wav_filename) for wav_filename in wav_filenames]
            
            print("splitting {} according to {}".format(wav_files, trans_file))
            
            origAudios = [wave.open(wav_file, "r") for wav_file in wav_files]
            
            # Loop over segments and split wav_file for each segment
            for segment in segments:
                # Create wav segment filename
                start_time = segment["start_time"]
                stop_time = segment["stop_time"]
                new_wav_filename = os.path.splitext(os.path.basename(trans_file))[0] + "-" + str(start_time) + "-" + str(stop_time) + ".wav"
                new_wav_file = os.path.join(target_dir, new_wav_filename)
                
                # If the wav segment filename does not exist create it
                if not os.path.exists(new_wav_file):
                    channel = 0 if segment["speaker"] == "A:" else 1
                    _split_wav(origAudios[channel], start_time, stop_time, new_wav_file)
            
            # Close origAudios
            for origAudio in origAudios:
                origAudio.close()

def _split_wav(origAudio, start_time, stop_time, new_wav_file):
    frameRate = origAudio.getframerate()
    origAudio.setpos(int(start_time*frameRate))
    chunkData = origAudio.readframes(int((stop_time - start_time)*frameRate))
    chunkAudio = wave.open(new_wav_file, "w")
    chunkAudio.setnchannels(origAudio.getnchannels())
    chunkAudio.setsampwidth(origAudio.getsampwidth())
    chunkAudio.setframerate(frameRate)
    chunkAudio.writeframes(chunkData)
    chunkAudio.close()

def _maybe_split_transcriptions(data_dir, original_data, converted_data):
    source_dir = os.path.join(data_dir, original_data)
    target_dir = os.path.join(data_dir, converted_data)
    
    if os.path.exists(os.path.join(source_dir, "split_transcriptions_done")):
        print("skipping maybe_split_transcriptions")
        return
    
    # Loop over transcription files and split them into individual files for
    # each utterance
    for root, dirnames, filenames in os.walk(source_dir):
        for filename in fnmatch.filter(filenames, "*.txt"):
            trans_file = os.path.join(root, filename)
            segments = _parse_transcriptions(trans_file)
            
            # Loop over segments and split wav_file for each segment
            for segment in segments:
                start_time = segment["start_time"]
                stop_time = segment["stop_time"]
                txt_filename = os.path.splitext(os.path.basename(trans_file))[0] + "-" + str(start_time) + "-" + str(stop_time) + ".txt"
                txt_file = os.path.join(target_dir, txt_filename)
                
                transcript = validate_label(segment["transcript"])
                
                # If the transcript is valid and the txt segment filename does
                # not exist create it
                if transcript != None and not os.path.exists(txt_file):
                    with open(txt_file, "w") as fout:
                        fout.write(transcript)
    
    with open(os.path.join(source_dir, "split_transcriptions_done"), "w") as fout:
        fout.write("This file signals to the importer than the transcription of this source dir has already been completed.")
    
def _maybe_split_sets(data_dir, original_data, converted_data):
    source_dir = os.path.join(data_dir, original_data)
    target_dir = os.path.join(data_dir, converted_data)
    
    filelist = sorted(glob(os.path.join(source_dir, "*.txt")))
    
    # We initially split the entire set into 80% train and 20% test, then
    # split the train set into 80% train and 20% validation.
    train_beg = 0
    train_end = int(0.8 * len(filelist))
    
    dev_beg = int(0.8 * train_end)
    dev_end = train_end
    train_end = dev_beg
    
    test_beg = dev_end
    test_end = len(filelist)
    
    _maybe_split_dataset(filelist[train_beg:train_end], os.path.join(target_dir, "train"))
    _maybe_split_dataset(filelist[dev_beg:dev_end], os.path.join(target_dir, "dev"))
    _maybe_split_dataset(filelist[test_beg:test_end], os.path.join(target_dir, "test"))
    
def _maybe_split_dataset(filelist, target_dir):
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
        for txt_file in filelist:
            new_txt_file = os.path.join(target_dir, os.path.basename(txt_file))
            os.rename(txt_file, new_txt_file)
            
            wav_file = os.path.splitext(txt_file)[0] + ".wav"
            new_wav_file = os.path.join(target_dir, os.path.basename(wav_file))
            os.rename(wav_file, new_wav_file)

def _read_data_set(graph, work_dir, data_set, thread_count, batch_size, numcep, numcontext):
    # Create data set dir
    dataset_dir = os.path.join(work_dir, data_set)
    
    # Obtain list of txt files
    txt_files = glob(os.path.join(dataset_dir, "*.txt"))
    
    # Return DataSet
    return DataSet(graph, txt_files, thread_count, batch_size, numcep, numcontext)

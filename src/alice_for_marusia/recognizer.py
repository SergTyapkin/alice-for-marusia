"""Measure live-paced streaming STT on the Capsule with a pre-recorded WAV."""

import array, ctypes as c, json, queue, threading, time, wave

P = c.c_char_p
I = c.c_int32
F = c.c_float


class Trans(c.Structure):
    _fields_ = [("encoder", P), ("decoder", P), ("joiner", P)]


class Para(c.Structure):
    _fields_ = [("encoder", P), ("decoder", P)]


class Single(c.Structure):
    _fields_ = [("model", P)]


class Model(c.Structure):
    _fields_ = [
        ("transducer", Trans),
        ("paraformer", Para),
        ("zipformer2_ctc", Single),
        ("tokens", P),
        ("num_threads", I),
        ("provider", P),
        ("debug", I),
        ("model_type", P),
        ("modeling_unit", P),
        ("bpe_vocab", P),
        ("tokens_buf", P),
        ("tokens_buf_size", I),
        ("nemo_ctc", Single),
        ("t_one_ctc", Single),
    ]


class Feature(c.Structure):
    _fields_ = [("sample_rate", I), ("feature_dim", I)]


class Fst(c.Structure):
    _fields_ = [("graph", P), ("max_active", I)]


class Hr(c.Structure):
    _fields_ = [("dict_dir", P), ("lexicon", P), ("rule_fsts", P)]


class Config(c.Structure):
    _fields_ = [
        ("feat_config", Feature),
        ("model_config", Model),
        ("decoding_method", P),
        ("max_active_paths", I),
        ("enable_endpoint", I),
        ("rule1", F),
        ("rule2", F),
        ("rule3", F),
        ("hotwords_file", P),
        ("hotwords_score", F),
        ("ctc", Fst),
        ("rule_fsts", P),
        ("rule_fars", P),
        ("blank_penalty", F),
        ("hotwords_buf", P),
        ("hotwords_buf_size", I),
        ("hr", Hr),
    ]


lib = c.CDLL("/tmp/sherpa-test/libsherpa-onnx-c-api.so")


def fn(name, args, result):
    f = getattr(lib, name)
    f.argtypes = args
    f.restype = result
    return f


ptr = c.c_void_p
create = fn("SherpaOnnxCreateOnlineRecognizer", [c.POINTER(Config)], ptr)
stream = fn("SherpaOnnxCreateOnlineStream", [ptr], ptr)
accept = fn("SherpaOnnxOnlineStreamAcceptWaveform", [ptr, I, c.POINTER(F), I], None)
ready = fn("SherpaOnnxIsOnlineStreamReady", [ptr, ptr], I)
decode = fn("SherpaOnnxDecodeOnlineStream", [ptr, ptr], None)
finish = fn("SherpaOnnxOnlineStreamInputFinished", [ptr], None)
result = fn("SherpaOnnxGetOnlineStreamResultAsJson", [ptr, ptr], ptr)
destroy_result = fn("SherpaOnnxDestroyOnlineStreamResultJson", [ptr], None)
drop_stream = fn("SherpaOnnxDestroyOnlineStream", [ptr], None)
drop_rec = fn("SherpaOnnxDestroyOnlineRecognizer", [ptr], None)
cfg = Config()
cfg.feat_config = Feature(16000, 80)
cfg.model_config.transducer = Trans(
    b"encoder.int8.onnx", b"decoder.onnx", b"joiner.int8.onnx"
)
cfg.model_config.tokens = b"tokens.txt"
cfg.model_config.num_threads = 2
cfg.model_config.provider = b"cpu"
cfg.decoding_method = b"greedy_search"
cfg.max_active_paths = 4

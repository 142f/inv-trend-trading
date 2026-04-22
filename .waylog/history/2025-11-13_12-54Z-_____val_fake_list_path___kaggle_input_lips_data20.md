#      val_fake_list_path: /kaggle/input/lips-data20
_Exported on 04/22/2026 at 17:13:45 GMT+8 from Alibaba Lingma via WayLog_


**User**

     val_fake_list_path: /kaggle/input/lips-data2000/val/val/1_fake	[default: /3240608030/val/1_fake]
       val_real_list_path: /kaggle/input/lips-data2000/val/val/0_real	[default: /3240608030/val/0_real]
                val_split: val                           
             weight_decay: 0.01                          
----------------- End -------------------


 85%|████████████████████████████████▉      | 752M/890M [00:10<00:01, 77.0MiB/s]/kaggle/working/LipFD-main/LipFD-main-base/models/clip/clip.py:59: UserWarning: /root/.cache/clip/ViT-L-14.pt exists, but the SHA256 checksum does not match; re-downloading the file
  warnings.warn(f"{download_target} exists, but the SHA256 checksum does not match; re-downloading the file")
100%|███████████████████████████████████████| 890M/890M [00:12<00:00, 72.4MiB/s]
  8%|██▊                                   | 67.0M/890M [00:05<00:22, 38.2MiB/s]训练过程中出现异常： Model has been downloaded but the SHA256 checksum does not not match
Traceback (most recent call last):
  File "/kaggle/working/LipFD-main/LipFD-main-base/train_ddp.py", line 135, in main
    temp_model = TrainerDDP(opt, rank)
                 ^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/trainer/trainer_ddp.py", line 27, in __init__
    self.model = build_model(opt.arch)
                 ^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/models/__init__.py", line 22, in build_model
    return LipFD(transformer_name[5:])
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/models/LipFD.py", line 15, in __init__
    self.encoder, self.preprocess = clip.load(name, device="cpu")
                                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/models/clip/clip.py", line 122, in load
    model_path = _download(_MODELS[name], download_root or os.path.expanduser("~/.cache/clip"))
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/models/clip/clip.py", line 72, in _download
    raise RuntimeError("Model has been downloaded but the SHA256 checksum does not not match")
RuntimeError: Model has been downloaded but the SHA256 checksum does not not match

---


**Alibaba Lingma**

用户提供了训练LipFD模型时遇到的CLIP模型SHA256校验和不匹配问题，具体表现为下载的`ViT-L-14.pt`文件校验失败，导致程序抛出异常。用户给出的错误日志显示在加载模型时触发了`RuntimeError`，并附带了完整的堆栈跟踪。

回答详细分析了问题原因，并提供了多种解决方案，包括清除缓存重新下载、临时修改代码跳过校验、手动上传模型文件以及增强代码鲁棒性的建议。同时强调了跳过校验可能带来的风险，推荐优先使用重新下载的方式解决问题。

此次对话由`common`类型的agent处理，内容聚焦于解决模型加载过程中的校验错误问题。

---


**User**

     val_fake_list_path: /kaggle/input/lips-data2000/val/val/1_fake	[default: /3240608030/val/1_fake]
       val_real_list_path: /kaggle/input/lips-data2000/val/val/0_real	[default: /3240608030/val/0_real]
                val_split: val                           
             weight_decay: 0.01                          
----------------- End -------------------


 85%|████████████████████████████████▉      | 752M/890M [00:10<00:01, 77.0MiB/s]/kaggle/working/LipFD-main/LipFD-main-base/models/clip/clip.py:59: UserWarning: /root/.cache/clip/ViT-L-14.pt exists, but the SHA256 checksum does not match; re-downloading the file
  warnings.warn(f"{download_target} exists, but the SHA256 checksum does not match; re-downloading the file")
100%|███████████████████████████████████████| 890M/890M [00:12<00:00, 72.4MiB/s]
  8%|██▊                                   | 67.0M/890M [00:05<00:22, 38.2MiB/s]训练过程中出现异常： Model has been downloaded but the SHA256 checksum does not not match
Traceback (most recent call last):
  File "/kaggle/working/LipFD-main/LipFD-main-base/train_ddp.py", line 135, in main
    temp_model = TrainerDDP(opt, rank)
                 ^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/trainer/trainer_ddp.py", line 27, in __init__
    self.model = build_model(opt.arch)
                 ^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/models/__init__.py", line 22, in build_model
    return LipFD(transformer_name[5:])
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/models/LipFD.py", line 15, in __init__
    self.encoder, self.preprocess = clip.load(name, device="cpu")
                                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/models/clip/clip.py", line 122, in load
    model_path = _download(_MODELS[name], download_root or os.path.expanduser("~/.cache/clip"))
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/kaggle/working/LipFD-main/LipFD-main-base/models/clip/clip.py", line 72, in _download
    raise RuntimeError("Model has been downloaded but the SHA256 checksum does not not match")
RuntimeError: Model has been downloaded but the SHA256 checksum does not not match

---


**Alibaba Lingma**

用户再次提出“重试”，针对之前遇到的CLIP模型SHA256校验失败问题，即加载`ViT-L-14.pt`时因校验和不匹配导致的`RuntimeError`。此前已提供多种解决方案，包括清除缓存、设置环境变量、修改下载逻辑、手动下载模型文件以及临时跳过校验。当前轮次回答进一步细化了手动清除缓存、设置下载超时、增强下载逻辑的重试机制、手动下载模型文件的具体步骤，并提供了临时跳过校验的代码示例，同时强调该方法存在安全风险。此次对话由`common`类型agent处理，内容聚焦于解决模型加载过程中的校验错误问题。

---

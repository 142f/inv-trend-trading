# 
系统上下文:

#file:e:\服务器代码\AiStation\LipFD-main-base\
_Exported on 04/22/2026 at 17:13:45 GMT+8 from Alibaba Lingma via WayLog_


**User**


系统上下文:

#file:e:\服务器代码\AiStation\LipFD-main-base\data\datasets.py
datasets.py
import cv2
import torch
import torchvision.transforms as transforms
from torch.utils.data import Dataset
import utils


class AVLip(Dataset):
    def __init__(self, opt):
        assert opt.data_label in ["train", "val"]
        self.data_label = opt.data_label
        self.real_list = utils.get_list(opt.real_list_path)
        self.fake_list = utils.get_list(opt.fake_list_path)
        self.label_dict = dict()
        for i in self.real_list:
            self.label_dict[i] = 0
        for i in self.fake_list:
            self.label_dict[i] = 1
        self.total_list = self.real_list + self.fake_list

        self.targets = [self.label_dict[path] for path in self.total_list]
    def __len__(self):
        return len(self.total_list)

    def __getitem__(self, idx):
        img_path = self.total_list[idx]
        label = self.label_dict[img_path]
        img = torch.tensor(cv2.imread(img_path), dtype=torch.float32)
        img = img.permute(2, 0, 1)
        crops = transforms.Normalize(mean=[0.48145466, 0.4578275, 0.40821073],
                                     std=[0.26862954, 0.26130258, 0.27577711])(img)
        # crop images
        # crops[0]: 1.0x, crops[1]: 0.65x, crops[2]: 0.45x
        crops = [[transforms.Resize((224, 224))(img[:, 500:, i:i + 500]) for i in range(5)], [], []]
        crop_idx = [(28, 196), (61, 163)]
        for i in range(len(crops[0])):
            crops[1].append(transforms.Resize((224, 224))
                            (crops[0][i][:, crop_idx[0][0]:crop_idx[0][1], crop_idx[0][0]:crop_idx[0][1]]))
            crops[2].append(transforms.Resize((224, 224))
                            (crops[0][i][:, crop_idx[1][0]:crop_idx[1][1], crop_idx[1][0]:crop_idx[1][1]]))
        img = transforms.Resize((1120, 1120))(img)

        return img, crops, label



用户上下文:

#selectedCode:e:\服务器代码\AiStation\LipFD-main-base\data\datasets.py#L1-L44
import cv2
import torch
import torchvision.transforms as transforms
from torch.utils.data import Dataset
import utils


class AVLip(Dataset):
    def __init__(self, opt):
        assert opt.data_label in ["train", "val"]
        self.data_label = opt.data_label
        self.real_list = utils.get_list(opt.real_list_path)
        self.fake_list = utils.get_list(opt.fake_list_path)
        self.label_dict = dict()
        for i in self.real_list:
            self.label_dict[i] = 0
        for i in self.fake_list:
            self.label_dict[i] = 1
        self.total_list = self.real_list + self.fake_list

        self.targets = [self.label_dict[path] for path in self.total_list]
    def __len__(self):
        return len(self.total_list)

    def __getitem__(self, idx):
        img_path = self.total_list[idx]
        label = self.label_dict[img_path]
        img = torch.tensor(cv2.imread(img_path), dtype=torch.float32)
        img = img.permute(2, 0, 1)
        crops = transforms.Normalize(mean=[0.48145466, 0.4578275, 0.40821073],
                                     std=[0.26862954, 0.26130258, 0.27577711])(img)
        # crop images
        # crops[0]: 1.0x, crops[1]: 0.65x, crops[2]: 0.45x
        crops = [[transforms.Resize((224, 224))(img[:, 500:, i:i + 500]) for i in range(5)], [], []]
        crop_idx = [(28, 196), (61, 163)]
        for i in range(len(crops[0])):
            crops[1].append(transforms.Resize((224, 224))
                            (crops[0][i][:, crop_idx[0][0]:crop_idx[0][1], crop_idx[0][0]:crop_idx[0][1]]))
            crops[2].append(transforms.Resize((224, 224))
                            (crops[0][i][:, crop_idx[1][0]:crop_idx[1][1], crop_idx[1][0]:crop_idx[1][1]]))
        img = transforms.Resize((1120, 1120))(img)

        return img, crops, label



问题：
Validation Progress: 0it [00:00, ?it/s]
Traceback (most recent call last):
  File "/3240608030/LipFD-main-base/train.py", line 173, in <module>
    ap, fpr, fnr, acc, auc = validate(model.model, val_loader, opt.gpu_ids)
  File "/3240608030/LipFD-main-base/validate.py", line 28, in validate
    ap = average_precision_score(y_true, y_pred_prob)
  File "/usr/local/lib/python3.10/dist-packages/sklearn/utils/_param_validation.py", line 211, in wrapper
    return func(*args, **kwargs)
  File "/usr/local/lib/python3.10/dist-packages/sklearn/metrics/_ranking.py", line 268, in average_precision_score
    return _average_binary_score(
  File "/usr/local/lib/python3.10/dist-packages/sklearn/metrics/_base.py", line 75, in _average_binary_score
    return binary_metric(y_true, y_score, sample_weight=sample_weight)
  File "/usr/local/lib/python3.10/dist-packages/sklearn/metrics/_ranking.py", line 230, in _binary_uninterpolated_average_precision
    precision, recall, _ = precision_recall_curve(
  File "/usr/local/lib/python3.10/dist-packages/sklearn/utils/_param_validation.py", line 184, in wrapper
    return func(*args, **kwargs)
  File "/usr/local/lib/python3.10/dist-packages/sklearn/metrics/_ranking.py", line 952, in precision_recall_curve
    fps, tps, thresholds = _binary_clf_curve(
  File "/usr/local/lib/python3.10/dist-packages/sklearn/metrics/_ranking.py", line 842, in _binary_clf_curve
    tps = stable_cumsum(y_true * weight)[threshold_idxs]
  File "/usr/local/lib/python3.10/dist-packages/sklearn/utils/extmath.py", line 1145, in stable_cumsum
    out.take(-1, axis=axis), expected, rtol=rtol, atol=atol, equal_nan=True
IndexError: cannot do a non-empty take from an empty axes.


---


**Alibaba Lingma**

用户报告了一个在验证过程中出现的 `IndexError`，具体发生在调用 `average_precision_score` 时，错误信息为 "cannot do a non-empty take from an empty axes"。该错误由 `y_true` 数据集中仅包含单一类别标签（全0或全1）导致，使得 `precision_recall_curve` 无法计算 PR 曲线。

用户提供的上下文包括对 `datasets.py` 和 `validate.py` 文件的引用，以及具体的错误堆栈信息。问题根源在于验证集配置文件可能为空，导致数据集中只包含真实样本或伪造样本，而没有同时包含两类样本。

回答中详细分析了问题原因，并提供了多个解决方案：  
1. 在 `AVLip` 类初始化时添加对 `real_list` 和 `fake_list` 的检查，确保两者不为空；  
2. 在 `validate.py` 中增加对 `y_true` 类别数的判断，避免调用 `average_precision_score` 时出错；  
3. 建议用户检查验证集配置文件，确保其包含有效样本路径，并在数据集初始化时增加强制检查。  

此外，还给出了调试建议，如在训练脚本中打印验证集统计信息，以及临时跳过问题批次的测试方案。  

本次对话由 `common` 类型的 agent 处理，主要围绕错误分析与修复方案展开。

---

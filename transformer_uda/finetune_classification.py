from datasets import load_dataset, load_metric
from transformers import InformerConfig, InformerForPrediction, PretrainedConfig, AdamW, get_scheduler#, Trainer, TrainingArguments

import torch
from torch.utils.data import DataLoader

import numpy as np
import pandas as pd
from pathlib import Path
import wandb
from tqdm.auto import tqdm
from functools import partial
import pdb
import argparse
import yaml
import shutil
from collections import Counter

from gluonts.time_feature import month_of_year

from transformer_uda.dataset_preprocess import create_train_dataloader, create_test_dataloader,create_transformation, convert_to_pandas_period, transform_start_field
from transformer_uda.informer_for_classification import InformerForSequenceClassification
from transformer_uda.huggingface_informer import get_dataset

WANDB_DIR = "/pscratch/sd/h/helenqu/sn_transformer/wandb/finetuning"
CACHE_DIR = "/pscratch/sd/h/helenqu/huggingface_datasets_cache"
CHECKPOINTS_DIR = "/pscratch/sd/h/helenqu/plasticc_all_gp_interp/models/finetuning_checkpoints"

class EarlyStopper:
    def __init__(self, patience=1, min_delta=0):
        self.patience = patience
        self.min_delta = min_delta
        self.counter = 0
        self.min_validation_loss = np.inf

    def early_stop(self, validation_loss):
        if validation_loss < self.min_validation_loss:
            self.min_validation_loss = validation_loss
            self.counter = 0
        elif validation_loss > (self.min_validation_loss + self.min_delta):
            self.counter += 1
            if self.counter >= self.patience:
                return True
        return False

def compute_metrics(eval_pred):
    logits, labels = eval_pred
    predictions = np.argmax(logits, axis=-1)
    return metric.compute(predictions=predictions, references=labels)

def normalize_data(example):
    # normalize data
    example["target"] = example["target"] / np.max(example["target"])
    return example

def train_loop(
        model: InformerForSequenceClassification,
        train_dataloader: torch.utils.data.DataLoader,
        val_dataloader: torch.utils.data.DataLoader,
        test_dataloader: torch.utils.data.DataLoader,
        optimizer: torch.optim.Optimizer,
        lr_scheduler: torch.optim.lr_scheduler._LRScheduler,
        device: torch.device,
        num_epochs: int,
        num_training_steps: int,
        log_interval=100,
        early_stopping=False,
        save_ckpts=True,
        dry_run=False,
        class_weights=None,
    ):

    progress_bar = tqdm(range(num_training_steps))
    metric= load_metric("accuracy")
    save_model_path = Path(CHECKPOINTS_DIR)
    if early_stopping:
        early_stopper = EarlyStopper(patience=3, min_delta=0.01)

    best_loss = np.inf
    for epoch in range(num_epochs):
        cumulative_loss = 0
        correct = 0
        for idx, batch in enumerate(train_dataloader):
            if class_weights is not None:
                batch['weights'] = torch.tensor(class_weights)
            batch = {k: v.to(device) for k, v in batch.items()}
            outputs = model(**batch)
            loss = outputs.loss
            loss.backward()

            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            progress_bar.update(1)

            cumulative_loss += loss.item()
            logits = outputs.logits
            predictions = torch.argmax(logits, dim=-1)
            correct += (predictions.flatten() == batch['labels']).sum().item()

        # if save_ckpts and cumulative_loss < best_loss:
        #     torch.save({
        #         'epoch': epoch,
        #         'model_state_dict': model.state_dict(),
        #         'optimizer_state_dict': optimizer.state_dict(),
        #         'loss': loss
        #     }, save_model_path / "best_model.pt") # load via torch.load

        val_loss, val_accuracy = validate(model, val_dataloader, device)

        test_loss, test_accuracy = validate(model, test_dataloader, device)

        if not dry_run:
            accuracy = (100 * correct) / (idx * batch['labels'].shape[0])
            wandb.log({
                'loss': cumulative_loss / idx,
                'accuracy': accuracy,
                'val_loss': val_loss,
                'val_accuracy': val_accuracy,
                'test_loss': test_loss,
                'test_accuracy': test_accuracy,
            })
        if early_stopping and early_stopper.early_stop(val_loss):
            break
    return model

def run_training_stage(stage, model, train_dataloader, val_dataloader, test_dataloader, config, device, dry_run=False, class_weights=None):
    if class_weights is not None:
        print("Using class weights")
    else:
        print("Not using class weights")

    if stage == 'lp':
        # freeze pretrained weights
        for name, param in model.named_parameters():
            if name.startswith('informer.encoder'):
                param.requires_grad = False
    else:
        # unfreeze all weights
        for name, param in model.named_parameters():
            param.requires_grad = True

    print(f"weight decay: {config['weight_decay']}")
    optimizer = AdamW(model.parameters(), lr=float(config[f'{stage}_lr']), weight_decay=config['weight_decay'])

    # ckpt = torch.load("/pscratch/sd/h/helenqu/plasticc/plasticc_all_gp_interp/finetuned_classification/pretrained_500k_context_170/torch_model/model.pt")
    # model.load_state_dict(ckpt['model_state_dict'])
    # optimizer.load_state_dict(ckpt['optimizer_state_dict'])

    num_training_steps = config['num_batches_per_epoch'] * config[f'{stage}_epochs']
    lr_scheduler = get_scheduler(
        "linear",
        optimizer=optimizer,
        num_warmup_steps=0,
        num_training_steps=num_training_steps
    )

    return train_loop(
        model=model,
        train_dataloader=train_dataloader,
        val_dataloader=val_dataloader,
        test_dataloader=test_dataloader,
        optimizer=optimizer,
        lr_scheduler=lr_scheduler,
        device=device,
        num_epochs=config[f'{stage}_epochs'],
        num_training_steps=num_training_steps,
        early_stopping=(stage == 'ft'),
        dry_run=dry_run,
        save_ckpts=False,
        class_weights=class_weights
    ), optimizer

def validate(model, dataloader, device):
    model.eval()
    cumulative_loss = 0
    correct = 0
    metric = load_metric("accuracy")

    for idx, batch in enumerate(dataloader):
        batch = {k: v.to(device) for k, v in batch.items()}
        with torch.no_grad():
            outputs = model(**batch)

        cumulative_loss += outputs.loss.item()
        logits = outputs.logits
        predictions = torch.argmax(logits, dim=-1)
        correct += (predictions.flatten() == batch['labels']).sum().item()
        metric.add_batch(predictions=predictions, references=batch["labels"])

    accuracy = (100 * correct) / (idx * batch['labels'].shape[0])
    return cumulative_loss / idx, metric.compute()

def save_model(model, optimizer, output_dir):
    print(f"Saving model to {output_dir}")
    if not Path(output_dir).exists():
        Path(output_dir).mkdir(parents=True)

    torch_model_dir = Path(output_dir) / "torch_model"
    hf_model_dir = Path(output_dir) / "hf_model"

    print(f"overwriting torch model at {torch_model_dir}")
    if torch_model_dir.exists():
        shutil.rmtree(torch_model_dir)
    torch_model_dir.mkdir(parents=True)

    print(f"overwriting hf model at {hf_model_dir}")
    if hf_model_dir.exists():
        shutil.rmtree(hf_model_dir)
    hf_model_dir.mkdir(parents=True)

    torch.save({
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
    }, torch_model_dir / "model.pt")

    model.save_pretrained(hf_model_dir)

def train(args, base_config, config=None):
    config.update(base_config)
    if not args.dry_run:
        wandb.init(config=config, name="dropout_0.5_wd_1e-3_ftlr_1e-5", project="finetuning-170", dir=WANDB_DIR) #name='finetune-classification-500k-pretrained-lpft', project='sn-transformer', dir=WANDB_DIR)
        config = wandb.config
    print(config)

    model_config = InformerConfig(
        # in the multivariate setting, input_size is the number of variates in the time series per time step
        input_size=6,
        # prediction length:
        prediction_length=config['prediction_length'],
        # context length:
        context_length=config['context_length'],
        # lags value copied from 1 week before:
        lags_sequence=config['lags'],
        time_features=config['time_features'],
        num_time_features=len(config['time_features']) + 1,
        dropout=config['dropout_rate'],
        encoder_layers=config['num_encoder_layers'],
        decoder_layers=config['num_decoder_layers'],
        d_model=config['d_model'],
        has_labels=True,
        num_labels=15,
        classifier_dropout=config['classifier_dropout'],
    )

    dataset = load_dataset(str(config['dataset_path']), cache_dir=CACHE_DIR)#, download_mode='force_redownload')
    abundances = Counter(dataset['train']['label'])
    max_count = max(abundances.values())
    class_weights = [max_count / abundances[i] if abundances[i] > 0 else 0 for i in range(15)]
    # print(f"Class weights applied: {class_weights}")

    train_dataloader = create_train_dataloader(
        config=model_config,
        dataset=dataset['train'],
        time_features=[month_of_year for x in config['time_features'] if 'month_of_year' in x],
        batch_size=config["batch_size"],
        num_batches_per_epoch=config["num_batches_per_epoch"],
        shuffle_buffer_length=1_000_000,
        allow_padding=False
    )
    val_dataloader = create_test_dataloader(
        config=model_config,
        dataset=dataset['validation'],
        time_features=[month_of_year for x in config['time_features'] if 'month_of_year' in x],
        batch_size=config["batch_size"],
        # num_batches_per_epoch=config["num_batches_per_epoch"],
        # shuffle_buffer_length=1_000_000,
        # allow_padding=False
    )
    test_dataset = get_dataset('/pscratch/sd/h/helenqu/plasticc/plasticc_all_gp_interp/examples', data_subset_file='/pscratch/sd/h/helenqu/plasticc/plasticc_all_gp_interp/examples/single_test_file.txt') # random file from plasticc test dataset
    labels = pd.read_csv("/pscratch/sd/h/helenqu/plasticc/plasticc_test_gp_interp/labels_7.csv")
    labels.loc[labels['label'] > 990, 'label'] = 99
    INT_LABELS = [90, 67, 52, 42, 62, 95, 15, 64, 88, 92, 65, 16, 53, 6, 99]
    labeled_test_dataset = test_dataset['train'].add_column('label', [INT_LABELS.index(labels[labels['object_id'] == int(objid)]['label'].values[0]) for objid in test_dataset['train']['objid']])

    test_dataloader = create_test_dataloader(
        config=model_config,
        dataset=labeled_test_dataset,
        time_features=[month_of_year for x in config['time_features'] if 'month_of_year' in x],
        batch_size=config["batch_size"],
        # num_batches_per_epoch=config["num_batches_per_epoch"],
        # shuffle_buffer_length=1_000_000,
        # allow_padding=False
    )

    model = InformerForSequenceClassification.from_pretrained(config['model_path'], config=model_config)

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model.to(device)

    # if args.load_finetuned_model:
    #     ckpt = torch.load(args.load_finetuned_model)
    #     model.load_state_dict(ckpt['model_state_dict'])
    #     optimizer.load_state_dict(ckpt['optimizer_state_dict'])

    model.train()
    if config['lp_epochs'] > 0:
        num_training_steps = config['lp_epochs'] * config['num_batches_per_epoch']
        model, _ = run_training_stage('lp', model, train_dataloader, val_dataloader, test_dataloader, config, device, dry_run=args.dry_run,) #class_weights=class_weights)
    num_training_steps = config['ft_epochs'] * config['num_batches_per_epoch']
    model, optimizer = run_training_stage('ft', model, train_dataloader, val_dataloader, test_dataloader, config, device, dry_run=args.dry_run,)# class_weights=class_weights)

    if args.save_model:
        save_model(model, optimizer, args.save_model)

    model.eval()
    metric= load_metric("accuracy")
    for batch in val_dataloader:
        batch = {k: v.to(device) for k, v in batch.items()}
        with torch.no_grad():
            outputs = model(**batch)

        logits = outputs.logits
        predictions = torch.argmax(logits, dim=-1)
        metric.add_batch(predictions=predictions, references=batch["labels"])

    print(metric.compute())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='create heatmaps from lightcurve data')
    # parser.add_argument('--num_epochs', type=int, help='absolute or relative path to your yml config file, i.e. "/user/files/create_heatmaps_config.yml"', required=True)
    parser.add_argument('--context_length', type=int, help='absolute or relative path to your yml config file, i.e. "/user/files/create_heatmaps_config.yml"')
    parser.add_argument('--load_model', type=str, help='absolute or relative path to your yml config file, i.e. "/user/files/create_heatmaps_config.yml"', required=True)
    parser.add_argument('--load_finetuned_model', type=str, help='absolute or relative path to your yml config file, i.e. "/user/files/create_heatmaps_config.yml"')
    parser.add_argument('--save_model', type=str, help='absolute or relative path to your yml config file, i.e. "/user/files/create_heatmaps_config.yml"')
    parser.add_argument('--dry_run', action='store_true', help='absolute or relative path to your yml config file, i.e. "/user/files/create_heatmaps_config.yml"')
    args = parser.parse_args()

    DATASET_PATH = Path('/pscratch/sd/h/helenqu/plasticc/train_with_labels')
    config={
        'prediction_length': 10 if args.context_length != 100 else 50,
        'context_length': args.context_length,
        'time_features': ['month_of_year'],
        'dataset_path': str(DATASET_PATH),
        'model_path': args.load_model,
        'batch_size': 32,
        'num_batches_per_epoch': 1000,
        # 'num_epochs': args.num_epochs,
        "dropout_rate": 0.2,
        "num_encoder_layers": 11,
        "num_decoder_layers": 7,
        "lags": [0],
        "d_model": 128,
        "freq": "1M",
        # 'lr': 1e-4,
        "pretrain_allow_padding": "no_pad" not in args.load_model,
    }
    # with open("/global/homes/h/helenqu/time_series_transformer/transformer_uda/finetune_hyperparameters.yml", 'r') as f:
    #     sweep_config = yaml.safe_load(f)
    with open("/global/homes/h/helenqu/time_series_transformer/transformer_uda/configs/context_170_best_finetune_hyperparams.yml", 'r') as f:
        ft_config = yaml.safe_load(f)
    ft_config['weight_decay'] = 0.001
    # ft_config['lp_lr'] = 5e-3
    # ft_config['lp_epochs'] = 25
    ft_config['ft_lr'] = 1e-5
    ft_config['classifier_dropout'] = 1

    # sweep_ids = {
    #     20: "4t28m5i4",
    #     100: "r2ejluf9",
    #     120: "y2ypjqxh",
    #     170: "9iikc81e"
    # }
    # sweep_id = f"helenqu/finetuning-500k-sweep-context-{args.context_length}/{sweep_ids[args.context_length]}"
    # sweep_id = wandb.sweep(sweep_config, project=f"finetuning-500k-sweep-context-{args.context_length}")
    # wandb.agent(sweep_id, partial(train, args, config), count=10)
    train(args, config, ft_config)

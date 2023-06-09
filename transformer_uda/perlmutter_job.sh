#!/bin/bash
#SBATCH -A dessn
#SBATCH -C gpu
#SBATCH -t 12:00:00
#SBATCH -N 1
#SBATCH -c 128
#SBATCH -q regular
#SBATCH --gpus=4

date
export SLURM_CPU_BIND="cores"
export HF_HOME="/pscratch/sd/h/helenqu/huggingface_datasets_cache"
export WANDB_DISABLE_SERVICE=true
module load python
module load pytorch/1.13.1
source activate pytorch-1.13.1
module load pytorch/1.13.1
# srun python \
accelerate launch --multi_gpu --num_processes=4 \
    /global/homes/h/helenqu/time_series_transformer/transformer_uda/transformer_uda/huggingface_informer.py \
    --data_dir /pscratch/sd/h/helenqu/plasticc/raw/plasticc_raw_examples \
    --save_model /pscratch/sd/h/helenqu/plasticc/plasticc_all_gp_interp/models/pretrained_all_masked \
    --fourier_pe \
    --mask \
    --num_epochs 15

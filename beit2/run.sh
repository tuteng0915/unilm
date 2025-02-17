python -m torch.distributed.launch --nproc_per_node=8 run_vqkd_training.py \
    --process_type laion \
    --train_interpolation bicubic \
    --min_crop_scale 0.08 \
    --model vqkd_encoder_base_decoder_3x768x12_clip \
    --teacher_input_size 224 \
    --codebook_n_emd 8192  \
    --codebook_emd_dim 32 \
    --quantize_kmeans_init \
    --rec_loss_type cosine \
    --batch_size 64 \
    --opt adamw \
    --opt_betas 0.9 0.99 \
    --lr 1e-3 \
    --weight_decay 1e-4  \
    --warmup_steps 4096 \
    --epochs 8 \
    --save_ckpt_freq 1 \
    --dataset-resampled \
# python -m torch.distributed.launch --nproc_per_node=1 run_vqkd_training.py \
#     --process_type laion \
#     --train_interpolation bicubic \
#     --min_crop_scale 0.08 \
#     --model vqkd_encoder_base_decoder_3x768x12_clip \
#     --teacher_input_size 224 \
#     --codebook_n_emd 8192  \
#     --codebook_emd_dim 32 \
#     --quantize_kmeans_init \
#     --rec_loss_type cosine \
#     --batch_size 4 \
#     --opt adamw \
#     --opt_betas 0.9 0.99 \
#     --weight_decay 1e-4  \
#     --warmup_epochs 4 \
#     --epochs 4 \
#     --save_ckpt_freq 20 
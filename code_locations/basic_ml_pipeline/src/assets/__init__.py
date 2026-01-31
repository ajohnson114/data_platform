from .train_lr import pull_data_from_postgres, fit_model, save_model

train_lr_assets = [pull_data_from_postgres, fit_model, save_model]

all_assets = [*train_lr_assets]
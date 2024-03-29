import os
import sys
import argparse
import torch
import itertools

from utils import set_seed, setup_logging, CfgNode as CN
from dataset import CharDataset
from trainer import Trainer
from model import Feedforward, zero_one_score
from gpt import GPT

def pick_best(models, data, loss_fn):
    inputs, targets = data
    scores = []
    for model in models:
        scores.append(model(inputs, targets, loss_fn)[1])
    return models[torch.argmax(torch.tensor(scores))]



def get_val_dataset(data_config, val_data):
    inputs, targets = [], []
    for idx in range(len(val_data)-data_config.block_size):
        inputs.append(val_data[idx:idx+data_config.block_size])
        targets.append(val_data[idx+1:idx+data_config.block_size+1])
    return (torch.tensor(inputs), torch.tensor(targets))


# For debugging purposes if you use IPDB, resolves a multiprocessing issue:
# https://stackoverflow.com/questions/45720153/python-multiprocessing-error-attributeerror-module-main-has-no-attribute
# You can safely ignore this
__spec__ = "ModuleSpec(name='builtins', loader=<class '_frozen_importlib.BuiltinImporter'>)"

# -----------------------------------------------------------------------------

def get_ff_config():
    C = CN()

    # system
    C.system = CN()
    C.system.seed = 3407
    C.system.work_dir = './out/chargpt'

    # model
    C.model = Feedforward.get_default_config()

    # trainer
    C.trainer = CN()
    C.trainer.device = 'auto'
    # dataloder parameters
    C.trainer.num_workers = 4
    # optimizer parameters
    C.trainer.max_iters = 50000
    C.trainer.batch_size = 64
    C.trainer.learning_rate = 5e-4
    C.trainer.betas = (0.9, 0.95)
    C.trainer.weight_decay = 0.1 # only applied on matmul weights
    C.trainer.grad_norm_clip = 1.0
    C.trainer.patience = 5
    C.trainer.validation_interval = 2000
    C.trainer.min_relative_improvement = 0.05

    return C

def get_gpt_config():
    C = CN()

    # system
    C.system = CN()
    C.system.seed = 3407
    C.system.work_dir = './out/chargpt'

    # model
    C.model = GPT.get_default_config()
    C.model.model_type = "gpt-nano"

    # trainer
    C.trainer = CN()
    C.trainer.device = 'auto'
    # dataloder parameters
    C.trainer.num_workers = 4
    # optimizer parameters
    C.trainer.max_iters = 50000
    C.trainer.batch_size = 64
    C.trainer.learning_rate = 5e-4
    C.trainer.betas = (0.9, 0.95)
    C.trainer.weight_decay = 0.1 # only applied on matmul weights
    C.trainer.grad_norm_clip = 1.0
    C.trainer.patience = 5
    C.trainer.validation_interval = 1000
    C.trainer.min_relative_improvement = 0.05


    return C


# ---------------------------------------------------------------------------------


def train(config, train_dataset, val_dataset, run_idx):
    print(config)
    setup_logging(config, run_idx)
    set_seed(config.system.seed)

    # construct the model
    config.model.vocab_size = train_dataset.get_vocab_size()
    config.model.block_size = train_dataset.get_block_size()
    if config.model.model_type == "feedforward":
        model = Feedforward(config.model)
    elif config.model.model_type.startswith("gpt"):
        config.vocab_size = train_dataset.get_vocab_size()
        config.block_size = train_dataset.get_block_size()
        model = GPT(config)
    

    # construct the trainer object
    trainer = Trainer(config.trainer, model, train_dataset, val_dataset=val_dataset)

    # iteration callback
    def batch_end_callback(trainer):

        if trainer.iter_num % 500 == 0:
            print(f"iter_dt {trainer.iter_dt * 1000:.2f}ms; iter {trainer.iter_num}: train loss {trainer.loss.item():.5f}")

        if trainer.iter_num % 5000 == 0:
            print("Generating Text: ")
            # evaluate both the train and test score
            model.eval()
            with torch.no_grad():
                # sample from the model...
                context = "O God, O God!"
                x = torch.tensor([train_dataset.stoi[s] for s in context], dtype=torch.long)[None,...].to(trainer.device)
                y = model.generate(x, 500, temperature=1.0, do_sample=True, top_k=10)[0]
                completion = ''.join([train_dataset.itos[int(i)] for i in y])
                print(completion)
            # save the latest model
            print("saving model")
            ckpt_path = os.path.join(config.system.work_dir, f"model_{run_idx}.pt")
            torch.save(model.state_dict(), ckpt_path)
            # revert model to training mode
            model.train()

    trainer.set_callback('on_batch_end', batch_end_callback)

    # run the optimization
    val_loss, val_perplexity = trainer.run()

    return model, val_loss, val_perplexity

if __name__ == '__main__':

    parser = argparse.ArgumentParser(
                    prog='tinyLM',
                    description='Train a small language model')
    parser.add_argument('-m', '--model', choices=["gpt", "feedforward"], default="feedforward", help="select the model type to train", type=str)
    args = parser.parse_args()

    data_config = CN()
    # If you make this bigger, make sure to give sufficient initial context to the generate method.
    data_config.block_size = 10

    # construct the entire dataset
    with open('data/input.txt', 'r') as f:
        data = f.read()

    # split dataset
    ratio = .7
    split_idx = int(len(data) * ratio)
    train_dataset = CharDataset(data_config, data[:split_idx])
    
    # The validation dataset can be evaluated all at once
    val_data = [train_dataset.stoi[x] for x in data[split_idx:]]
    val_dataset = get_val_dataset(data_config, val_data)

    # Set hyperparameter search space
    learning_rates = [2e-4, 3e-4]
    hidden_dims = [200, 300, 400]
    n_embds = [48, 96]
    hyperparameters_list = itertools.product(learning_rates, hidden_dims, n_embds)


    # Train a model for each combination of hyperparameters
    best_model = None
    best_val_loss = float('inf')
    best_val_perplexity = float('inf')
    best_hyps = None

    for (run_idx, (learning_rate, hidden_dim, n_embd)) in enumerate(hyperparameters_list):
        
        if args.model == "feedforward":
            config = get_ff_config()
        
        elif args.model == "gpt":
            config = get_gpt_config()

        
        config.model.learning_rate = learning_rate
        config.model.hidden_dim = hidden_dim
        config.model.n_embd = n_embd

        model, val_loss, val_perplexity = train(config, train_dataset, val_dataset, run_idx)
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_model = model
            best_hyps = (learning_rate, hidden_dim, n_embd)
            best_val_perplexity = val_perplexity

    print(f"Best Perplexity: {best_val_perplexity:.5f} Best Validation Loss: {best_val_loss:.5f} Learning Rate: {learning_rate} Hidden Dim: {hidden_dim} Embed Dim: {n_embd}")
    
    # Report results
    # final_accuracy = best_model(val_dataset[0], val_dataset[1], zero_one_score)[1] / len(val_dataset[0])
    # print(f"Final accuracy of best model: {final_accuracy.tolist()}")


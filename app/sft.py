"""Launch the training job inside a container."""
import os
import random
from typing import Any, Generator, Tuple

import torch
from datasets import Dataset, load_dataset, load_from_disk
from fire import Fire
from huggingface_hub import login
from peft import LoraConfig, get_peft_model
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, GenerationConfig, TrainingArguments
from transformers.integrations import WandbCallback
from trl import SFTTrainer
from wandb import Table, finish

from utils import DatasetMover, dump_envs, load_config, peft_module_casting_to_bf16

CHECKPOINT_DIR = "/mnt/checkpoint"
DATASET_DIR = "/mnt/dataset"
MAX_NEW_TOKENS = 512


def training_generator(
    dataset: str,
    split: str = "train",
    from_disk: bool = False,
    format: str = "text",
    bos_token: str = "<s>",
    eos_token: str = "</s>",
) -> Generator[dict[str, Any], dict[str, Any], None]:
    """Generate training data by yielding each row in the dataset split."""
    # We assume that the dataset is a HuggingFace dataset, and a DatasetDict
    # such that the dict has train, val, and test splits.
    if from_disk:
        ds = load_from_disk(dataset)
        ds = ds[split]
        # Iterate through the dataset and yield each row
        print(f"{ds.num_rows} rows in {split} split")
    else:
        ds = load_dataset(dataset, streaming=True, split=split)

    for row in iter(ds):
        if format == "text":
            text = f"{row['text']}"
            if not text.startswith(bos_token):
                text = f"{bos_token}{text}{eos_token}"
            yield {"text": text}
        elif format == "completion":
            # If the dataset is a 'completion' format dataset, we need to concatenate the prompt and completion
            text = f"{row['prompt']}{row['completion']}"
            if not text.startswith(bos_token):
                text = f"{bos_token}{text}{eos_token}"
            yield {
                "text": text,
                "prompt": row["prompt"],
                "completion": row["completion"],
            }
        else:
            raise Exception(f"Unknown format: {format}")


def fetch_dataset(config: dict[str, Any], bos_token: str, eos_token: str) -> Tuple[Dataset, Dataset, Dataset]:
    """Fetch the dataset from HuggingFace Hub or S3."""
    if config["dataset"]["type"] == "hf":
        if os.environ.get("HF_TOKEN", None):
            login(token=os.environ["HF_TOKEN"])
        train_split = Dataset.from_generator(
            training_generator,
            gen_kwargs={
                "dataset": config["dataset"]["name"],
                "split": "train",
                "format": config["dataset"].get("format", "text"),
                "bos_token": bos_token,
                "eos_token": eos_token,
            },
        )
        try:
            val_split = Dataset.from_generator(
                training_generator,
                gen_kwargs={
                    "dataset": config["dataset"]["name"],
                    "split": "val",
                    "format": config["dataset"].get("format", "text"),
                    "bos_token": bos_token,
                    "eos_token": eos_token,
                },
            )
        except:  # pylint: disable=bare-except  # noqa: E722
            print("Unable to create val dataset")
            val_split = None
        try:
            test_split = Dataset.from_generator(
                training_generator,
                gen_kwargs={
                    "dataset": config["dataset"]["name"],
                    "split": "test",
                    "format": config["dataset"].get("format", "text"),
                    "bos_token": bos_token,
                    "eos_token": eos_token,
                },
            )
        except:  # pylint: disable=bare-except  # noqa: E722
            print("Unable to create test dataset")
            test_split = None
    elif config["dataset"]["type"] == "s3":
        os.makedirs(DATASET_DIR)
        dataset_mover = DatasetMover()
        local_name = config["dataset"]["name"][config["dataset"]["name"].find("/") + 1 :]
        dataset_mover.download(
            bucket_name=config["dataset"]["name"].split("/")[0],
            object_name=f"{local_name}.tar.gz",
            output_folder_path=DATASET_DIR,
        )
        print(os.listdir(DATASET_DIR))
        print(os.listdir(f"{DATASET_DIR}/{local_name}"))
        train_split = Dataset.from_generator(
            training_generator,
            gen_kwargs={
                "dataset": f"{DATASET_DIR}/{local_name}",
                "split": "train",
                "from_disk": True,
                "format": config["dataset"].get("format", "text"),
                "bos_token": bos_token,
                "eos_token": eos_token,
            },
        )
        try:
            val_split = Dataset.from_generator(
                training_generator,
                gen_kwargs={
                    "dataset": f"{DATASET_DIR}/{local_name}",
                    "split": "val",
                    "from_disk": True,
                    "format": config["dataset"].get("format", "text"),
                    "bos_token": bos_token,
                    "eos_token": eos_token,
                },
            )
        except:  # pylint: disable=bare-except  # noqa: E722
            print("Unable to create val dataset")
            val_split = None
        try:
            test_split = Dataset.from_generator(
                training_generator,
                gen_kwargs={
                    "dataset": f"{DATASET_DIR}/{local_name}",
                    "split": "test",
                    "from_disk": True,
                    "format": config["dataset"].get("format", "text"),
                    "bos_token": bos_token,
                    "eos_token": eos_token,
                },
            )
        except:  # pylint: disable=bare-except  # noqa: E722
            print("Unable to create test dataset")
            test_split = None
    else:
        raise ValueError(f"Unknown dataset_type: {config['dataset']['type']}")
    return train_split, val_split, test_split


def load_model(config: dict[str, Any]) -> Tuple[AutoModelForCausalLM, AutoTokenizer]:
    """Load the model from HuggingFace Hub or S3."""
    use_4bit = config["training"].get("peft", {}).pop("quantized", False)

    if use_4bit:
        # Compute dtype for 4-bit base models
        bnb_4bit_compute_dtype = "float16"
        # Quantization type (fp4 or nf4)
        bnb_4bit_quant_type = "nf4"
        # Activate nested quantization for 4-bit base models (double quantization)
        use_nested_quant = False

        # Load tokenizer and model with QLoRA configuration
        compute_dtype = getattr(torch, bnb_4bit_compute_dtype)

        bnb_config = BitsAndBytesConfig(
            load_in_4bit=use_4bit,
            bnb_4bit_quant_type=bnb_4bit_quant_type,
            bnb_4bit_compute_dtype=compute_dtype,
            bnb_4bit_use_double_quant=use_nested_quant,
        )

        # Check GPU compatibility with bfloat16
        if compute_dtype == torch.float16 and use_4bit:
            major, _ = torch.cuda.get_device_capability()
            if major >= 8:
                print("=" * 80)
                print("Your GPU supports bfloat16: accelerate training with bf16=True")
                print("=" * 80)

    if config["model"]["base"]["type"] == "hf":
        if os.environ.get("HF_TOKEN", None):
            login(token=os.environ["HF_TOKEN"])

        if use_4bit:
            # Load base model
            model = AutoModelForCausalLM.from_pretrained(
                config["model"]["base"]["name"], quantization_config=bnb_config, device_map={"": 0}
            )
            model.config.use_cache = False
            model.config.pretraining_tp = 1
        else:
            model = AutoModelForCausalLM.from_pretrained(
                config["model"]["base"]["name"],
                torch_dtype=torch.bfloat16,
                use_cache=False,
                trust_remote_code=True,
            )
        tokenizer = AutoTokenizer.from_pretrained(config["model"]["base"]["name"], trust_remote_code=True)
        # May need to have some custom padding logic here
        special_tokens = {"pad_token": "[PAD]"}
        tokenizer.add_special_tokens(special_tokens)
        if "additional_tokens" in config.get("tokenizer", {}):
            tokenizer.add_tokens(config["tokenizer"]["additional_tokens"])
        tokenizer.padding_side = "right"
        model.config.pad_token_id = tokenizer.pad_token_id
        model.resize_token_embeddings(len(tokenizer))

    elif config["model"]["base"]["type"] == "s3":
        # TODO : Add s3 support
        raise NotImplementedError("S3 support not implemented yet")
    else:
        raise ValueError(f"Unknown base_model_type: {config['model']['base']['type']}")
    return model, tokenizer


def freeze(model: AutoModelForCausalLM, n_freeze: int, freeze_embed: bool, module_name: str = "layers") -> None:
    """Freeze the model layers for SFT without PEFT."""
    if n_freeze > 0:

        def _find_mod(model: AutoModelForCausalLM, module_name: str) -> Any:
            for name, mod in model.named_modules():
                if name.endswith(module_name):
                    return mod

        # freeze layers (disable gradients)
        for param in model.parameters():
            param.requires_grad = False

        # never freeze the head
        for param in model.lm_head.parameters():
            param.requires_grad = True

        layers = _find_mod(model, module_name)
        for param in layers[n_freeze:].parameters():
            param.requires_grad = True

    # Freeze embeddings for small memory decrease
    if freeze_embed:
        embed_tokens = _find_mod(model, "embed_tokens")
        embed_tokens.weight.requires_grad_(False)


class LLMSampleCB(WandbCallback):  # type: ignore
    """Callback for sampling from a LLM and reporting custom eval to WANDB."""

    def __init__(
        self: "LLMSampleCB",
        trainer: SFTTrainer,
        format: str,
        test_split: Dataset,
        num_samples: int = 100,
        max_new_tokens: int = MAX_NEW_TOKENS,
        log_model: str = "checkpoint",
        run_tests_str: str = "",
        run_metrics_str: str = "",
    ):
        """Initialize the callback by extracting a few rows from the test split."""
        super().__init__()
        assert format == "completion", "Only completion format supported for now"
        self._log_model = log_model
        self.model, self.tokenizer = trainer.model, trainer.tokenizer
        self.gen_config = GenerationConfig.from_pretrained(trainer.model.name_or_path, max_new_tokens=max_new_tokens)

        # Sample a few rows from the test split to generate a table of predictions
        # for visual inspection a.k.a. spot checking
        # Randomly select indices for the samples
        selected_indices = random.sample(range(test_split.num_rows), num_samples)
        # Retrieve the selected samples from the dataset
        test_split_list = list(test_split)
        self.sample_split = []
        for i in selected_indices:
            self.sample_split.append(test_split_list[i])
        self.sample_split = Dataset.from_list(self.sample_split)

        self.run_tests_str = run_tests_str
        self.run_metrics_str = run_metrics_str

        # Test the provided code if present:
        print("Testing custom code, if provided")
        test_rows = []
        for example in tqdm(self.sample_split, leave=False):
            prompt = example["prompt"]
            actual = example["completion"]
            test_rows.append({"prompt": prompt, "actual": actual, "predicted": actual, "initial": actual})
        self.execute_custom_code(test_rows)

    def initialize(self: "LLMSampleCB") -> None:
        """Generate initial predictions for the sample split and log them to WANDB."""
        self._wandb.init()

        print("Generating initial predictions for sample split")
        self.initial_predictions = []
        for example in tqdm(self.sample_split, leave=False):
            prompt = example["prompt"]
            if not prompt.startswith(self.tokenizer.bos_token):
                prompt = f"{self.tokenizer.bos_token}{prompt}"
            predicted = self.generate(prompt=prompt)
            self.initial_predictions.append(predicted)

        # Generate the table of sample predictions
        records_table, metrics = self.samples_table_and_metrics()

        # Log the table of sample predictions to W&B
        self._wandb.log({"sample_predictions": records_table})

        # Log the calculated metrics to W&B
        self._wandb.log(metrics)
        print("LLMSampleCB initialized")

    def execute_custom_code(self, rows: list[dict[str, Any]]) -> Tuple[Table, dict[str, Any]]:
        """Execute custom code for tests and metrics."""
        records_table = Table(columns=["prompt", "predicted", "actual", "initial", "test_result", "errors"])

        # Assuming run_tests_str and run_metrics_str contain your testing and metrics code respectively

        print("Updating records_table with predictions, test results, and errors")
        if self.run_tests_str and os.environ.get("ALLOW_CUSTOM_TESTS", "false").lower() == "true":
            # Execute dynamic code for tests
            def run_tests(prompts: list[str], predictions: list[str]) -> Tuple[list[bool], list[str]]:
                return [False] * len(prompts), [""] * len(prompts)

            print("Running custom tests")
            exec(self.run_tests_str, globals())
            tests, errors = run_tests([row["prompt"] for row in rows], [row["predicted"] for row in rows])  # noqa: F821
        else:
            print("Skipping custom tests")
            tests, errors = [False] * len(rows), [""] * len(rows)

        if self.run_metrics_str and os.environ.get("ALLOW_CUSTOM_METRICS", "false").lower() == "true":
            # Execute dynamic code for metrics
            def run_metrics(prompts: list[str], actuals: list[str], predictions: list[str]) -> dict[str, float]:
                return {}

            print("Running custom metrics")
            exec(self.run_metrics_str, globals())
            metrics = run_metrics(
                [row["prompt"] for row in rows],
                [row["actual"] for row in rows],
                [row["predicted"] for row in rows],
            )
        else:
            print("Skipping custom metrics")
            metrics = {}

        index = 0
        passed = 0
        for row in tqdm(rows, leave=False):
            test_result = "PASS" if tests[index] else "FAIL"
            passed += 1 if test_result == "PASS" else 0
            error_message = errors[index] if index < len(errors) else ""
            records_table.add_data(
                row["prompt"],
                row["predicted"],
                row["actual"],
                row["initial"],
                test_result,
                error_message,
            )
            index += 1

        metrics["passed"] = passed * 100 / len(rows)
        print("Metrics:", metrics)

        return records_table, metrics

    def generate(self: "LLMSampleCB", prompt: str) -> Any:
        """Generate a completion from a prompt."""
        tokenized_prompt = self.tokenizer(prompt, return_tensors="pt", padding=True)["input_ids"].cuda()
        with torch.inference_mode():
            output = self.model.generate(inputs=tokenized_prompt, generation_config=self.gen_config)
        return self.tokenizer.decode(output[0][len(tokenized_prompt[0]) :], skip_special_tokens=True)

    def samples_table_and_metrics(self) -> Tuple[Table, dict[str, Any]]:
        """Generate a table of predictions for visual inspection and evaluate them."""
        print("Generating predictions for sample split")
        current_predictions = []
        for example in tqdm(self.sample_split, leave=False):
            prompt = example["prompt"]
            if not prompt.startswith(self.tokenizer.bos_token):
                prompt = f"{self.tokenizer.bos_token}{prompt}"
            predicted = self.generate(prompt=prompt)
            current_predictions.append(predicted)

        # Generate rows of predictions
        rows = []
        for example, current, initial in tqdm(
            zip(self.sample_split, current_predictions, self.initial_predictions), leave=False
        ):
            prompt = example["prompt"]
            actual = example["completion"]
            predicted = current
            rows.append({"prompt": prompt, "actual": actual, "predicted": predicted, "initial": initial})
            # print(f"Prompt: {prompt}\nActual: {actual}\nPredicted: {predicted}\nInitial: {initial}\n")

        return self.execute_custom_code(rows)

    def on_evaluate(self, args: Any, state: Any, control: Any, **kwargs: dict[str, Any]) -> None:
        """Log the sample predictions and metrics to WANDB on eval callback."""
        super().on_evaluate(args, state, control, **kwargs)

        # Generate the table of sample predictions
        records_table, metrics = self.samples_table_and_metrics()

        # Log the table of sample predictions to W&B
        self._wandb.log({"sample_predictions": records_table})

        # Log the calculated metrics to W&B
        self._wandb.log(metrics)


def train(
    train_split: Dataset,
    val_split: Dataset,
    test_split: Dataset,
    model: AutoModelForCausalLM,
    tokenizer: AutoTokenizer,
    config: dict[str, Any],
) -> None:
    """Start training the model as defined by the config."""
    # Assumes model is a causal language model
    model.config.use_cache = False

    # Calculate max steps from num epochs
    if "num_train_epochs" in config["training"]["sft"]:
        raise ValueError("num_train_epochs is not supported, use max_steps instead")
    assert "max_steps" in config["training"]["sft"], "max_steps must be defined"

    # SFT training config
    config["training"]["sft"]["save_total_limit"] = 2
    config["training"]["sft"]["save_strategy"] = "steps"
    config["training"]["sft"]["load_best_model_at_end"] = True
    sft_config = TrainingArguments(output_dir=CHECKPOINT_DIR, **config["training"]["sft"])

    # PEFT training config
    if "peft" in config["training"]:
        peft_config = LoraConfig(**config["training"]["peft"])
        model = get_peft_model(model, peft_config)
        if "bf16" in config["training"]["sft"]:
            peft_module_casting_to_bf16(model, config["training"]["sft"])
        model.print_trainable_parameters()
        sft_config.peft_config = peft_config
        sft_config.n_freeze = "all"
    else:
        n_freeze = config["training"]["other"].get("n_freeze", 24)
        freeze_embed = config["training"]["other"].get("freeze_embed", True)
        freeze(model, n_freeze, freeze_embed)
        peft_config = None

    trainer = SFTTrainer(
        tokenizer=tokenizer,
        model=model,
        train_dataset=train_split,
        eval_dataset=val_split,
        peft_config=peft_config,
        dataset_text_field="text",
        max_seq_length=config["training"]["trainer"]["max_seq_length"],
        packing=config["training"]["trainer"]["packing"],  # Should you combine multiple examples into one sequence?
        args=sft_config,
    )

    format = config["dataset"].get("format", "text")
    if test_split and test_split.num_rows > 0 and format == "completion":
        # we instantiate the W&B callback with the trainer object and the dataset we want to sample from
        wandb_callback = LLMSampleCB(
            trainer,
            format,
            test_split,
            num_samples=100,
            max_new_tokens=config["training"]["trainer"]["max_seq_length"],
            run_tests_str=config.get("tests", ""),
            run_metrics_str=config.get("metrics", ""),
        )
        wandb_callback.initialize()
        trainer.add_callback(wandb_callback)

    print("Starting training")
    trainer.train()

    # distributed training config
    if trainer.is_fsdp_enabled:
        trainer.accelerator.state.fsdp_plugin.set_state_dict_type("FULL_STATE_DICT")

    # if test_split and test_split.num_rows > 0:
    #     trainer.evaluate(test_split)


def save_model(model: Any, tokenizer: Any, config: dict[str, Any]) -> None:
    """Save the model to a local directory."""
    print("Saving model and tokenizer")
    local_name = config["model"]["output"]["name"].split("/")[-1]
    model.save_pretrained(local_name)
    tokenizer.save_pretrained(local_name)
    print(os.listdir(local_name))

    """Upload the model to HuggingFace Hub or S3."""
    if os.getenv("RANK", "0") != "0":
        return
    if "output" not in config["model"]:
        return
    if config["model"]["output"]["type"] == "hf":
        print("Saving model and tokenizer to hf")
        if os.environ.get("HF_TOKEN", None):
            login(token=os.environ["HF_TOKEN"])
        model.push_to_hub(local_name)
        tokenizer.push_to_hub(local_name)
    elif config["model"]["output"]["type"] == "s3":
        # TODO : Add s3 support
        raise NotImplementedError("S3 support not implemented yet")


def main() -> None:
    """Execute the main training loop."""
    dump_envs()
    config = load_config()
    print("Loading model")
    model, tokenizer = load_model(config)
    print("Loading dataset")
    train_split, val_split, test_split = fetch_dataset(
        config=config, bos_token=tokenizer.bos_token, eos_token=tokenizer.eos_token
    )

    print("Starting training")
    train(
        train_split=train_split,
        val_split=val_split,
        test_split=test_split,
        model=model,
        tokenizer=tokenizer,
        config=config,
    )
    print("Save and Uploading model..")
    save_model(model, tokenizer, config)

    finish()


if __name__ == "__main__":
    Fire(main)

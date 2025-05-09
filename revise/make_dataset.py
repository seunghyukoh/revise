import inspect
import json
import os
from typing import Callable, Union

from datasets import Dataset, load_dataset

from revise.evaluators.comparison_evaluator import (
    BaseComparisonEvaluator,
    GSM8KEvaluator,
)
from revise.generators.vllm_generator import VllmGenerationParams, VllmGenerator
from revise.prompts import prepare_batch_chat_messages_fns, prepare_chat_messages_fns
from revise.utils import configure_logging, hash_params

logger = configure_logging(level="info")


def generate_and_evaluate(
    model_path: str,
    dataset: Dataset,
    evaluator: Union[BaseComparisonEvaluator],
    prepare_batch_chat_messages_fn: Callable,
    max_new_tokens=1024,
    temperature=0.7,
    num_completions=10,
    top_p=0.9,
    num_examples=-1,
    seed=42,
    question_key="question",
    answer_key="answer",
    prediction_key="prediction",
    ignore_cache=False,
):
    params = dict(
        model_path=model_path,
        dataset=str(dataset),
        evaluator_source=inspect.getsource(evaluator.__class__),
        prepare_batch_chat_messages_source=inspect.getsource(
            prepare_batch_chat_messages_fn
        ),
        max_new_tokens=max_new_tokens,
        temperature=temperature,
        num_completions=num_completions,
        top_p=top_p,
        num_examples=num_examples,
        seed=seed,
        question_key=question_key,
        answer_key=answer_key,
        prediction_key=prediction_key,
    )
    hashed = hash_params(params)
    cache_path = f"./.cache/{hashed}.jsonl"
    os.makedirs(".cache", exist_ok=True)
    if os.path.exists(cache_path) and not ignore_cache:
        logger.info(f"Loading dataset from cache: {cache_path}")
        with open(cache_path, "r") as f:
            return Dataset.from_list([json.loads(line) for line in f])

    # Load dataset
    if question_key not in dataset.column_names:
        raise ValueError(f"Dataset must contain a '{question_key}' column.")
    if answer_key not in dataset.column_names:
        raise ValueError(f"Dataset must contain a '{answer_key}' column.")

    if num_examples > 0:
        dataset = dataset.select(range(num_examples))

    # Make chat messages
    batch_chat_messages = prepare_batch_chat_messages_fn(dataset)

    # Get generator
    generation_params = VllmGenerationParams(
        max_new_tokens=max_new_tokens,
        temperature=temperature,
        num_completions=num_completions,
        top_p=top_p,
        seed=seed,
        skip_special_tokens=False,
    )
    generator = VllmGenerator(model=model_path, gen_params=generation_params)

    # Generate
    predictions = generator.chat(batch_chat_messages)  # List[List[str]]

    # Evaluate
    new_dataset = []

    questions = dataset[question_key]
    gt_answers = dataset[answer_key]
    for question, gt_answer, prediction_set in zip(questions, gt_answers, predictions):
        num_predictions = len(prediction_set)
        scores = evaluator.run(
            answers=[gt_answer] * num_predictions,
            predictions=prediction_set,
            return_results=True,
        )
        score_list = scores["score_list"]

        for prediction, is_correct in zip(prediction_set, score_list):
            new_dataset.append(
                {
                    question_key: question,
                    answer_key: gt_answer,
                    prediction_key: prediction,
                    "is_correct": is_correct,
                }
            )

    new_dataset = Dataset.from_list(new_dataset)
    with open(cache_path, "w") as f:
        data = new_dataset.to_list()
        for line in data:
            f.write(json.dumps(line) + "\n")

    return new_dataset


def make_dataset(
    dataset,
    question_key,
    answer_key,
    prediction_key,
    prepare_chat_messages_fn: Callable,
    is_verifier=False,
    use_gt: bool = False,
    rethink_token="<|reserved_special_token_0|>",
):
    if question_key not in dataset.column_names:
        raise ValueError(f"Dataset must contain a '{question_key}' column.")

    if answer_key not in dataset.column_names:
        raise ValueError(f"Dataset must contain a '{answer_key}' column.")

    if prediction_key not in dataset.column_names:
        raise ValueError(f"Dataset must contain a '{prediction_key}' column.")

    if "is_correct" not in dataset.column_names:
        raise ValueError("Dataset must contain a 'is_correct' column.")

    new_dataset = []

    for sample in dataset:
        question = sample[question_key]
        gt_answer = sample[answer_key]

        prediction = sample[prediction_key]
        prediction = prediction.split(rethink_token)[-1]

        is_correct = sample["is_correct"]

        if is_correct:
            chosen_message = prediction
            rejected_message = prediction + rethink_token
        else:
            chosen_message = prediction + rethink_token
            if not is_verifier:
                chosen_message += gt_answer
            rejected_message = prediction

        user_messages = prepare_chat_messages_fn(question)
        chosen = user_messages + [{"role": "assistant", "content": chosen_message}]
        rejected = user_messages + [{"role": "assistant", "content": rejected_message}]

        new_dataset.append(
            {
                question_key: question,
                answer_key: gt_answer,
                prediction_key: prediction,
                "is_correct": is_correct,
                "is_verifier": is_verifier,
                "chosen": chosen,
                "rejected": rejected,
            }
        )

    if use_gt:
        # Use gt answer as prediction
        for sample in dataset:
            question = sample[question_key]
            gt_answer = sample[answer_key]

            chosen_message = gt_answer
            rejected_message = gt_answer + rethink_token

            user_messages = prepare_chat_messages_fn(question)
            chosen = user_messages + [{"role": "assistant", "content": chosen_message}]
            rejected = user_messages + [
                {"role": "assistant", "content": rejected_message}
            ]

            new_dataset.append(
                {
                    question_key: question,
                    answer_key: gt_answer,
                    prediction_key: gt_answer,
                    "is_correct": True,
                    "is_verifier": is_verifier,
                    "is_gt": True,
                    "chosen": chosen,
                    "rejected": rejected,
                }
            )

    new_dataset = Dataset.from_list(new_dataset)
    return new_dataset


if __name__ == "__main__":
    from datasets import DatasetDict
    from prompts import prepare_batch_chat_messages_fns

    evaluator = GSM8KEvaluator(mode="flexible")

    dataset = load_dataset("openai/gsm8k", name="main")
    train_dataset = dataset["train"]
    test_dataset = dataset["test"]

    prepare_batch_chat_messages_fn = prepare_batch_chat_messages_fns["gsm8k"]
    prepare_chat_messages_fn = prepare_chat_messages_fns["gsm8k"]

    question_key = "question"
    answer_key = "answer"
    prediction_key = "prediction"

    train_num_examples = 100
    test_num_examples = 10

    train_num_completions = 4
    test_num_completions = 1

    seed = 42

    new_train_dataset_evaluated = generate_and_evaluate(
        model_path="meta-llama/llama-3.2-1b-instruct",
        evaluator=evaluator,
        dataset=train_dataset,
        prepare_batch_chat_messages_fn=prepare_batch_chat_messages_fn,
        num_examples=train_num_examples,
        num_completions=train_num_completions,
        seed=seed,
        question_key=question_key,
        answer_key=answer_key,
        prediction_key=prediction_key,
    )

    new_test_dataset_evaluated = generate_and_evaluate(
        model_path="meta-llama/llama-3.2-1b-instruct",
        evaluator=evaluator,
        dataset=test_dataset,
        prepare_batch_chat_messages_fn=prepare_batch_chat_messages_fn,
        num_examples=test_num_examples,
        num_completions=test_num_completions,
        seed=seed,
        question_key=question_key,
        answer_key=answer_key,
        prediction_key=prediction_key,
    )

    new_train_dataset = make_dataset(
        dataset=new_train_dataset_evaluated,
        question_key=question_key,
        answer_key=answer_key,
        prediction_key=prediction_key,
        prepare_chat_messages_fn=prepare_chat_messages_fn,
    )

    new_test_dataset = make_dataset(
        dataset=new_test_dataset_evaluated,
        question_key=question_key,
        answer_key=answer_key,
        prediction_key=prediction_key,
        prepare_chat_messages_fn=prepare_chat_messages_fn,
    )

    new_dataset = DatasetDict(
        {
            "train": new_train_dataset,
            "test": new_test_dataset,
        }
    )

    new_dataset.push_to_hub("JakeOh/testtesttest")

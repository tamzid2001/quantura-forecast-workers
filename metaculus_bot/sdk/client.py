# Retained from forecasting-tools 0.3.4 (MIT); see LICENSE and provenance.json.
from __future__ import annotations
import copy
import logging
from typing import Literal
from .questions import BinaryQuestion, NumericQuestion, DiscreteQuestion, MultipleChoiceQuestion, DateQuestion, ConditionalQuestion, MetaculusQuestion
logger = logging.getLogger(__name__)
GroupQuestionMode = Literal["exclude", "unpack_subquestions"]

class DataOrganizer:
    @classmethod
    def get_question_from_post_json(cls, post_json: dict) -> MetaculusQuestion:
        assert "question" in post_json, "Question key not found in API JSON"
        question_type_string = post_json["question"]["type"]  # type: ignore
        if question_type_string == BinaryQuestion.get_api_type_name():
            question_type = BinaryQuestion
        elif question_type_string == NumericQuestion.get_api_type_name():
            question_type = NumericQuestion
        elif question_type_string == DiscreteQuestion.get_api_type_name():
            question_type = DiscreteQuestion
        elif question_type_string == MultipleChoiceQuestion.get_api_type_name():
            question_type = MultipleChoiceQuestion
        elif question_type_string == DateQuestion.get_api_type_name():
            question_type = DateQuestion
        elif question_type_string == ConditionalQuestion.get_api_type_name():
            question_type = ConditionalQuestion
        else:
            raise ValueError(f"Unknown question type: {question_type_string}")
        question = question_type.from_metaculus_api_json(post_json)
        return question

class MetaculusClient:
    def _post_json_to_questions_while_handling_groups(
        self, post_json_from_api: dict, group_question_mode: GroupQuestionMode
    ) -> list[MetaculusQuestion]:
        if "group_of_questions" in post_json_from_api:
            if group_question_mode == "exclude":
                logger.debug(
                    f"Excluding group question post {post_json_from_api['id']}"
                )
                return []
            elif group_question_mode == "unpack_subquestions":
                if "group_of_questions" in post_json_from_api:
                    logger.debug(
                        f"Unpacking subquestions for group question post {post_json_from_api['id']}"
                    )
                    questions = self._unpack_group_question(post_json_from_api)
                    return questions
            else:
                raise ValueError("group_question_mode option not supported")
        elif "conditional" in post_json_from_api:
            post_json_from_api["question"] = post_json_from_api["conditional"]
            post_json_from_api["question"]["type"] = "conditional"
        return [DataOrganizer.get_question_from_post_json(post_json_from_api)]

    @staticmethod
    def _unpack_group_question(post_json_from_api: dict) -> list[MetaculusQuestion]:
        group_json: dict = post_json_from_api["group_of_questions"]
        questions: list[MetaculusQuestion] = []
        question_jsons: list[dict] = group_json["questions"]
        question_ids: list[int] = [q["id"] for q in question_jsons]
        for question_json in question_jsons:
            # Reformat the json to make it look like a normal post
            new_question_json = copy.deepcopy(question_json)
            new_question_json["fine_print"] = group_json["fine_print"]
            new_question_json["description"] = group_json["description"]
            new_question_json["resolution_criteria"] = group_json["resolution_criteria"]

            new_post_json = copy.deepcopy(post_json_from_api)
            new_post_json["question"] = new_question_json

            question_obj = DataOrganizer.get_question_from_post_json(new_post_json)
            question_obj.question_ids_of_group = question_ids.copy()
            questions.append(question_obj)
        return questions

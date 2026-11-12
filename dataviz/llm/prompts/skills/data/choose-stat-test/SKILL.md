---
name: choose-stat-test
when: the user asks whether a difference or a relationship is real
description: Pick and read a statistic test
---
1. Number by two groups: ttest, or mannwhitney when skewed. By several groups: anova. Two categories: chi2. Two numbers: pearson, or spearman for ranks.
2. `profile_result` first: small or skewed groups change the choice.
3. Run `stat_test`. Report the effect size beside the p-value: with many rows a tiny gap is still "significant".
4. A correlation does not show cause.

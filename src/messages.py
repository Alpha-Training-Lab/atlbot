from config import MAIN_GROUP_LINK
# ===========================================================================
# ----- APPROVED MESSAGE ------------------------------------------------------
WELCOME_BODY = """Your registration has been approved.🎉 Welcome to Alpha Training Lab.
Your Leverage to the better life you seek.

Before you join, these are the rules. They apply in every ATL group, to every member — leaders included. No one is exempt.

YOUR PROFILE
1. Your profile picture must be a clear photo of you — not distorted, not unclear, not your feet, not a group photo, not someone else.
2. Your display name must show your real first name and surname. Not a nickname.
3. Do not change your username without telling the admin team first.

CONDUCT
4. Respect everyone, in what you say and how you approach people.
5. Do not spam the groups with unnecessary messages or links.
6. No airdrops or links of that kind.
7. No buying or selling of goods or services in the groups.
8. Do not ask members for money. ATL is not a bank or a lender.
9. Do not share ATL group links with anyone who is not yet a member.

TAKING PART
10. Join the other classes, read up, and practise what you learn.
11. Ask questions — for your own benefit and everyone else's.
12. Let the admins know if you will be away for a long period.

Moderation is handled by the ATL administration with the help of Alpha(@AlphaTrainingLab_bot). Breaking a rule brings a warning first. If the warning is ignored, it leads to removal."""

WELCOME_APPROVED = f"""{WELCOME_BODY}

You are now free to join the main group:
{MAIN_GROUP_LINK}

Welcome aboard."""


def welcome_approved(invite_link=None):
  tail = (f"You are now free to join the main group:\n{invite_link}\n\n"
          "This link is yours alone and expires in 48 hours."
          if invite_link else
          "An admin will send you your group link shortly.")
  return f"{WELCOME_BODY}\n\n{tail}\n\nWelcome aboard."


# ----- INDUCTION GROUP ------------------------------------------------------
WELCOME_INDUCTION = """Welcome, {name}. You're at the ATL Induction Center.

Alpha Training Lab is a non-profit community. Membership is free, permanent, and no one here will ever ask you for money.

Here's what to do:
1. Read the induction material from the top. Start here: {url}
2. Take your time. It's a long read and it isn't meant to be finished in a day — most people spend one to two weeks on it.
3. You would find instructions what to do when you done reading the induction material. Follow them carefully.
4. An admin will review you, then I'll take you through registration privately.

Questions while you read? Message me directly — I'm @AlphaTrainingLab_bot."""


# ------ INDUCTION POST INCOMPLETE ------------------------------------------------------
INDUCTION_POST_INCOMPLETE = """Thanks {name} — I've seen your message, but it isn't complete, so I can't pass it to the onboarding team yet.

What's wrong: to move to the next phase, your post has to tag relevant members, not only me. You've missed at least one of them. The handles are named in the pinned instructions, and knowing who they are is part of what you pick up while reading.

What to do:
1. Read the induction material from the top. Start here: {url}
2. Look out for the most recent instruction on how to join the group, make sure you follow them carefully, and make sure you tag the relevant members in your post.
3. Post here once more, tagging me and every admin listed there.

Nothing is lost and there's no penalty — post again as soon as you're ready, and I'll pick it straight up. If you get stuck, message me directly and I'll walk you through it."""
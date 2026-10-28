from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class UserBase(BaseModel):
    # Username: 3–30 chars, alphanumeric + underscore only
    username: str = Field(min_length=3, max_length=30, pattern=r"^[a-zA-Z0-9_]+$")
    email: EmailStr


class UserCreate(UserBase):
    # Minimum 8 chars prevents trivially weak passwords
    password: str = Field(min_length=8, max_length=128)
    country: Optional[str] = Field(None, max_length=100)


class UserLogin(BaseModel):
    username: str = Field(max_length=254)  # generous; just prevents megabyte payloads
    password: str = Field(max_length=128)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str = Field(max_length=2048)
    password: str = Field(min_length=8, max_length=128)


class User(BaseModel):
    id: int
    username: str
    email: EmailStr
    profile_image_url: Optional[str] = None
    bio: Optional[str] = None
    is_verified_critic: bool = False
    onboarding_completed: bool = True
    country: Optional[str] = None
    is_premium: bool = False

    model_config = {"from_attributes": True}


class ChatStatusResponse(BaseModel):
    is_premium: bool
    daily_limit: int
    used_today: int
    remaining: int
    ads_watched_today: int
    ads_remaining: int
    premium_daily_limit: int = 0


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None


class ProfileUpdate(BaseModel):
    username: str
    profile_image_url: Optional[str] = None
    bio: Optional[str] = None
    favorite_genres: list[str] = []
    favorite_movie: Optional["FavoriteMovieInput"] = None
    is_private_account: bool = False
    notification_preferences: "NotificationPreferences"
    current_password: Optional[str] = None
    new_password: Optional[str] = None


class FriendRequest(BaseModel):
    username: str


class FavoriteMovieInput(BaseModel):
    movie_id: int
    title: str
    poster_path: Optional[str] = None
    release_date: Optional[str] = None


class FavoritePersonInput(BaseModel):
    person_id: int
    name: str
    profile_path: Optional[str] = None
    role: Optional[str] = None


class OnboardingPayload(BaseModel):
    favorite_genres: list[str]
    favorite_movies: list[FavoriteMovieInput]
    preferred_decades: list[str]
    preferred_languages: list[str]
    preferred_countries: list[str]
    favorite_people: list[FavoritePersonInput]
    watch_frequency: str


class OnboardingState(BaseModel):
    favorite_genres: list[str]
    favorite_movies: list[FavoriteMovieInput]
    preferred_decades: list[str]
    preferred_languages: list[str]
    preferred_countries: list[str]
    favorite_people: list[FavoritePersonInput]
    watch_frequency: str


class SearchMovieResult(BaseModel):
    id: int
    title: str
    poster_path: Optional[str] = None
    release_date: Optional[str] = None
    genre_ids: list[int] = []


class SearchPersonResult(BaseModel):
    id: int
    name: str
    profile_path: Optional[str] = None
    known_for_department: Optional[str] = None


class TrendingMovieCategory(BaseModel):
    key: str
    title: str
    subtitle: str
    items: list[SearchMovieResult]


class SearchResultsResponse(BaseModel):
    query: str
    type: str
    movies: list[SearchMovieResult]
    users: list["FeedUserSummary"]
    clubs: list["ClubSummary"]
    lists: list["MovieListSummary"]


class HashtagTrend(BaseModel):
    tag: str
    post_count: int


class TrendingResponse(BaseModel):
    movies: list[TrendingMovieCategory]
    users: list["FeedUserSummary"]
    clubs: list["ClubSummary"]
    lists: list["MovieListSummary"]
    trending_posts: list["FeedPost"] = []
    trending_hashtags: list[HashtagTrend] = []


class FeedCommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=1000)


class FeedPostCreate(BaseModel):
    movie_id: int
    movie_title: str = Field(max_length=500)
    movie_poster_path: Optional[str] = Field(None, max_length=500)
    movie_release_date: Optional[str] = Field(None, max_length=20)
    text_content: str = Field(max_length=2000)
    sentiment: Optional[str] = Field(None, max_length=100)
    publish_to_feed: bool = True
    contains_spoiler: bool = False
    would_watch_again: Optional[bool] = None


class FeedShareCreate(BaseModel):
    friend_username: str = Field(max_length=30)


class ConversationCreate(BaseModel):
    title: Optional[str] = Field(None, max_length=100)
    member_usernames: list[str] = Field(max_length=50)  # max 50 participants


class DirectConversationCreate(BaseModel):
    friend_username: str = Field(max_length=30)


class DirectMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class FeedUserSummary(BaseModel):
    id: int
    username: str
    profile_image_url: Optional[str] = None
    is_verified_critic: bool = False
    is_following: bool = False


class NotificationResponse(BaseModel):
    id: int
    notification_type: str
    preference_key: Optional[str] = None
    message: str
    is_read: bool
    created_at: datetime
    actor: FeedUserSummary
    post_id: Optional[int] = None
    comment_id: Optional[int] = None
    club_id: Optional[int] = None
    movie_id: Optional[int] = None
    conversation_id: Optional[int] = None
    request_id: Optional[int] = None
    deep_link: Optional[str] = None


class NotificationReadPayload(BaseModel):
    is_read: bool = True


class NotificationPreferences(BaseModel):
    post_likes: bool = True
    direct_messages: bool = True
    following_posts: bool = True
    new_followers: bool = True
    follow_request_approved: bool = True
    post_comments: bool = True
    comment_replies: bool = True
    mentions: bool = True
    club_updates: bool = True


class FeedComment(BaseModel):
    id: int
    content: str
    created_at: datetime
    author: FeedUserSummary


class FeedPost(BaseModel):
    id: int
    post_type: str
    text_content: Optional[str] = None
    sentiment: Optional[str] = None
    visible_in_feed: bool = True
    contains_spoiler: bool = False
    would_watch_again: Optional[bool] = None
    tags: list[str]
    club_id: Optional[int] = None
    club_name: Optional[str] = None
    movie_id: Optional[int] = None
    movie_title: Optional[str] = None
    movie_poster_path: Optional[str] = None
    movie_release_date: Optional[str] = None
    list_title: Optional[str] = None
    list_items: list[str]
    created_at: datetime
    author: FeedUserSummary
    review_headline: Optional[str] = None
    rating: Optional[float] = None
    like_count: int
    comment_count: int
    share_count: int
    liked_by_me: bool
    saved_by_me: bool
    comments: list[FeedComment]


class FeedResponse(BaseModel):
    items: list[FeedPost]
    next_cursor: Optional[str] = None


class HashtagSuggestion(BaseModel):
    tag: str


class MentionSuggestion(BaseModel):
    username: str
    profile_image_url: Optional[str] = None


class MovieListSummary(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    visibility: str
    cover_image_url: Optional[str] = None
    slug: Optional[str] = None
    allow_social_sharing: bool = False
    owner_username: Optional[str] = None
    updated_at: datetime
    item_count: int
    poster_paths: list[str]


class MovieListItemDetail(BaseModel):
    id: int
    movie_id: int
    title: str
    poster_path: Optional[str] = None
    release_date: Optional[str] = None
    position: int
    added_at: datetime


class MovieListItemCreate(BaseModel):
    movie_id: int
    title: str
    poster_path: Optional[str] = None
    release_date: Optional[str] = None


class MovieListItemsCreate(BaseModel):
    movies: list[MovieListItemCreate]


class MovieListReorderPayload(BaseModel):
    item_ids: list[int]


class MovieListDetail(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    visibility: str
    cover_image_url: Optional[str] = None
    slug: Optional[str] = None
    allow_social_sharing: bool = False
    owner_username: Optional[str] = None
    can_edit: bool = False
    created_at: datetime
    updated_at: datetime
    item_count: int
    items: list[MovieListItemDetail]


class MovieListCreate(BaseModel):
    title: str
    description: Optional[str] = None
    visibility: str
    allow_social_sharing: bool = False


class MovieListUpdate(BaseModel):
    title: str
    description: Optional[str] = None
    visibility: str
    allow_social_sharing: bool = False


class LikeResponse(BaseModel):
    liked: bool
    like_count: int


class SaveResponse(BaseModel):
    saved: bool


class FollowResponse(BaseModel):
    following: bool
    pending: bool = False
    user: FeedUserSummary


class ShareResponse(BaseModel):
    message: str
    recipient: FeedUserSummary


class RatedMovie(BaseModel):
    id: int
    movie_id: int
    title: str
    poster_path: Optional[str] = None
    release_date: Optional[str] = None
    rating: float
    rated_at: datetime

    model_config = {"from_attributes": True}


class MovieRatingCreate(BaseModel):
    title: str
    poster_path: Optional[str] = None
    release_date: Optional[str] = None
    rating: float


class FriendSummary(BaseModel):
    username: str
    profile_image_url: Optional[str] = None


class FollowUserSummary(BaseModel):
    id: int
    username: str
    profile_image_url: Optional[str] = None
    is_verified_critic: bool = False


class BadgeSummary(BaseModel):
    title: str
    description: str
    locked: bool = True


class ProfileTopGenre(BaseModel):
    name: str
    count: int
    percentage: int


class ProfileMoodSummary(BaseModel):
    title: str
    description: str
    signal: str


class ProfileStats(BaseModel):
    average_rating: float = 0
    rated_movies_count: int = 0
    top_genres: list[ProfileTopGenre] = []
    mood: ProfileMoodSummary


class ProfileResponse(BaseModel):
    user: User
    bio: Optional[str] = None
    preferred_genres: list[str] = []
    favorite_movie: Optional[FavoriteMovieInput] = None
    is_private_account: bool = False
    notification_preferences: NotificationPreferences = NotificationPreferences()
    follower_count: int = 0
    following_count: int = 0
    followers: list[FollowUserSummary] = []
    following: list[FollowUserSummary] = []
    is_following: bool = False
    follow_request_pending: bool = False
    can_view_private_profile: bool = True
    private_profile_message: Optional[str] = None
    recent_ratings: list[RatedMovie]
    top_ratings: list[RatedMovie]
    posts: list[FeedPost]
    saved_posts: list[FeedPost]
    public_lists: list[MovieListSummary] = []
    notifications: list[NotificationResponse]
    badges: list[BadgeSummary]
    friends: list[FriendSummary]
    stats: ProfileStats


class FollowConnectionListResponse(BaseModel):
    username: str
    kind: str
    total: int
    items: list[FollowUserSummary]


class FriendshipResponse(BaseModel):
    message: str
    friend: FriendSummary


class MessageResponse(BaseModel):
    message: str
    reset_link: Optional[str] = None


class DirectParticipant(BaseModel):
    id: int
    username: str
    profile_image_url: Optional[str] = None


class DirectMessage(BaseModel):
    id: int
    content: Optional[str] = None
    created_at: datetime
    sender: DirectParticipant
    shared_post: Optional[FeedPost] = None


class ConversationSummary(BaseModel):
    id: int
    title: str
    is_group: bool
    participants: list[DirectParticipant]
    last_message_preview: str
    updated_at: datetime
    unread_count: int = 0


class ConversationDetail(BaseModel):
    id: int
    title: str
    is_group: bool
    participants: list[DirectParticipant]
    messages: list[DirectMessage]
    has_more: bool = False


class ConversationMessagesResponse(BaseModel):
    messages: list[DirectMessage]
    has_more: bool = False


class ClubBase(BaseModel):
    name: str = Field(min_length=3, max_length=60)
    short_description: str = Field(min_length=10, max_length=140)
    description: Optional[str] = Field(None, max_length=1200)
    genres: list[str] = Field(default=[], max_length=10)
    cover_image_url: Optional[str] = Field(None, max_length=500)
    is_private: bool = False


class ClubCreate(ClubBase):
    pass


class ClubCreatorSummary(BaseModel):
    id: int
    username: str
    profile_image_url: Optional[str] = None


class ClubSummary(BaseModel):
    id: int
    name: str
    slug: str
    short_description: str
    description: Optional[str] = None
    genres: list[str] = []
    cover_image_url: Optional[str] = None
    is_private: bool
    member_count: int
    creator: ClubCreatorSummary
    joined_by_me: bool = False
    pending_request_by_me: bool = False
    created_at: datetime


class ClubSearchResponse(BaseModel):
    my_clubs: list[ClubSummary]
    discover_clubs: list[ClubSummary]


class ClubJoinResponse(BaseModel):
    message: str
    joined: bool = False
    request_pending: bool = False
    club: ClubSummary


class ClubMemberSummary(BaseModel):
    id: int
    username: str
    profile_image_url: Optional[str] = None
    role: str
    joined_at: datetime


class ClubJoinRequestSummary(BaseModel):
    id: int
    created_at: datetime
    requester: ClubCreatorSummary


class ClubMemberRoleUpdate(BaseModel):
    role: str


class ClubChallengeProgress(BaseModel):
    id: str
    title: str
    description: str
    badge_name: str
    target_value: int
    progress_value: int
    completed: bool
    participant_count: int = 0


class ClubRankingEntry(BaseModel):
    rank: int
    user: ClubCreatorSummary
    role: str
    badge_count: int
    score: int


class ClubDetailResponse(BaseModel):
    club: ClubSummary
    is_member: bool
    can_view_full_content: bool = True
    can_manage: bool
    current_user_role: Optional[str] = None
    members: list[ClubMemberSummary]
    pending_requests: list[ClubJoinRequestSummary]
    challenges: list[ClubChallengeProgress]
    ranking: list[ClubRankingEntry]
    posts: list[FeedPost]
    posts_next_cursor: Optional[str] = None


class CriticApplicationCreate(BaseModel):
    motivation: str = Field(min_length=50, max_length=3000)
    portfolio_url: Optional[str] = Field(None, max_length=500)


class CriticApplicationResponse(BaseModel):
    id: int
    status: str
    motivation: str
    portfolio_url: Optional[str] = None
    created_at: datetime
    reviewed_at: Optional[datetime] = None
    applicant: Optional["FeedUserSummary"] = None

    model_config = {"from_attributes": True}


class AdminCriticApplicationReview(BaseModel):
    approve: bool


class CriticSummary(BaseModel):
    id: int
    username: str
    profile_image_url: Optional[str] = None
    bio: Optional[str] = None
    review_count: int = 0
    follower_count: int = 0

    model_config = {"from_attributes": True}


class CriticListResponse(BaseModel):
    critics: list[CriticSummary]
    total: int


class AdminCriticTogglePayload(BaseModel):
    is_verified_critic: bool


class GlobalRankingEntry(BaseModel):
    rank: int
    user: FeedUserSummary
    rated_count: int
    post_count: int
    follower_count: int
    badge_count: int
    score: int


class GlobalRankingResponse(BaseModel):
    entries: list[GlobalRankingEntry]
    total: int


class MovieWatchStatusResponse(BaseModel):
    in_watchlist: bool
    in_watched: bool
    rating: Optional[float] = None


class MarkWatchedPayload(BaseModel):
    movie_id: int
    title: str
    poster_path: Optional[str] = None
    release_date: Optional[str] = None


class TrendingPostEntry(BaseModel):
    post: FeedPost
    engagement_score: int


class PostReportCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ReportedPostSummary(BaseModel):
    id: int
    author: Optional["FeedUserSummary"] = None
    text_content: Optional[str] = None
    movie_title: Optional[str] = None
    created_at: datetime


class PostReportResponse(BaseModel):
    id: int
    post_id: int
    club_id: int
    reason: str
    created_at: datetime
    reporter: "FeedUserSummary"
    post: Optional[ReportedPostSummary] = None


class RecommendationItem(BaseModel):
    movie_id: int
    title: Optional[str] = None
    poster_path: Optional[str] = None
    release_date: Optional[str] = None
    vote_average: Optional[float] = None
    genres: list[str] = []
    overview: Optional[str] = None


class RecommendationItemV2(BaseModel):
    movie_id: int
    title: Optional[str] = None
    poster_path: Optional[str] = None
    release_date: Optional[str] = None
    vote_average: Optional[float] = None
    genres: list[str] = []
    overview: Optional[str] = None
    match_score: Optional[int] = None
    reason_tags: list[str] = []


class RecommendationRow(BaseModel):
    row_id: str
    title: str
    reason: Optional[str] = None
    movies: list[RecommendationItemV2] = []
    is_sponsored: bool = False
    sponsor_label: Optional[str] = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    # O servidor ainda corta para as últimas mensagens (ver chatbot.MAX_HISTORY_MESSAGES).
    history: list[ChatMessage] = Field(default=[], max_length=50)


class PremiumStatusResponse(BaseModel):
    is_premium: bool


class ChatMovieCard(BaseModel):
    movie_id: int
    title: Optional[str] = None
    year: Optional[int] = None
    director: Optional[str] = None
    genres: list[str] = []
    vote_average: Optional[float] = None
    overview: Optional[str] = None
    poster_path: Optional[str] = None


class ChatResponse(BaseModel):
    message: str
    movies: list[ChatMovieCard] = []


SearchResultsResponse.model_rebuild()
TrendingResponse.model_rebuild()
ProfileUpdate.model_rebuild()
ProfileResponse.model_rebuild()
CriticApplicationResponse.model_rebuild()
PostReportResponse.model_rebuild()
ReportedPostSummary.model_rebuild()
